"""
LoL → Telegram Notifier
=======================
Фоновое приложение для Windows: следит за запуском League of Legends и
рассылает сообщение друзьям в Telegram через Bot API.

Режимы запуска:
    python notifier.py                # запустить трей-приложение (основной режим)
    python notifier.py --settings      # открыть окно настроек
    python notifier.py --collect-ids  # собрать chat_id друзей, нажавших Start у бота
    python notifier.py --send-now      # разослать сообщение прямо сейчас (тест)
    python notifier.py --check         # проверить токен и список получателей

Конфиг — рядом в config.json.
"""

import sys
import json
import time
import threading
from pathlib import Path

import requests

CONFIG_PATH = Path(__file__).with_name("config.json")
API = "https://api.telegram.org/bot{token}/{method}"

# Режимы игры: ключ → (подпись в UI)
GAME_MODES = [
    ("ranked", "🏆 Ранкед"),
    ("aram", "💫 АРАМ"),
    ("arena", "⚔️ Арена"),
]
MODE_LABELS = dict(GAME_MODES)


# --------------------------------------------------------------------------- #
#  Конфиг
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(f"Не найден config.json по пути {CONFIG_PATH}")
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def active_message(cfg: dict) -> str:
    """Текст для текущего выбранного режима игры."""
    mode = cfg.get("game_mode", "ranked")
    msgs = cfg.get("messages", {})
    return msgs.get(mode) or cfg.get("message", "")


def active_recipients(cfg: dict) -> list:
    """Список получателей с учётом режима 'всем' / 'выбрать вручную'."""
    rs = cfg.get("recipients", [])
    if cfg.get("send_to", "all") == "all":
        return rs
    result = []
    for r in rs:
        if isinstance(r, dict):
            if r.get("selected", True):
                result.append(r)
        else:
            result.append(r)
    return result


# --------------------------------------------------------------------------- #
#  Telegram Bot API
# --------------------------------------------------------------------------- #
def tg_call(token: str, method: str, **params):
    url = API.format(token=token, method=method)
    r = requests.post(url, data=params, timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error на {method}: {data}")
    return data["result"]


def send_message(token: str, chat_id, text: str):
    return tg_call(token, "sendMessage", chat_id=chat_id, text=text)


def broadcast(cfg: dict) -> tuple[int, int]:
    """Разослать сообщение активным получателям. Возвращает (успешно, всего)."""
    token = cfg["bot_token"]
    recipients = active_recipients(cfg)
    text = active_message(cfg)
    delay = float(cfg.get("send_delay_sec", 1.5))

    mode_label = MODE_LABELS.get(cfg.get("game_mode", "ranked"), "?")
    scope = "всем (Avengers)" if cfg.get("send_to", "all") == "all" else "выбранным"
    print(f"Рассылка [{mode_label}] → {scope}: {len(recipients)} получателей")

    ok = 0
    for r in recipients:
        chat_id = r["chat_id"] if isinstance(r, dict) else r
        name = r.get("name", chat_id) if isinstance(r, dict) else chat_id
        try:
            send_message(token, chat_id, text)
            ok += 1
            print(f"  ✅ отправлено: {name}")
        except Exception as e:
            print(f"  ❌ ошибка для {name}: {e}")
        time.sleep(delay)  # пауза между сообщениями — бережём аккаунт от флуд-лимитов
    print(f"Итог: {ok}/{len(recipients)} доставлено.")
    return ok, len(recipients)


# --------------------------------------------------------------------------- #
#  Сбор chat_id (кто нажал Start у бота)
# --------------------------------------------------------------------------- #
def collect_ids(cfg: dict):
    token = cfg["bot_token"]
    print("Опрашиваю Telegram… Попроси друзей открыть бота и нажать Start.")
    print("Ctrl+C — закончить и сохранить.\n")

    known = {r["chat_id"] if isinstance(r, dict) else r for r in cfg.get("recipients", [])}
    found: dict[int, str] = {}
    offset = None
    try:
        while True:
            updates = tg_call(token, "getUpdates", offset=offset, timeout=25)
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue
                chat = msg["chat"]
                if chat["type"] != "private":
                    continue
                cid = chat["id"]
                name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
                    or chat.get("username") or str(cid)
                if cid not in found and cid not in known:
                    found[cid] = name
                    print(f"  + {name}  (chat_id={cid})")
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    if not found:
        print("\nНовых получателей не найдено.")
        return

    cfg.setdefault("recipients", [])
    for cid, name in found.items():
        cfg["recipients"].append({"chat_id": cid, "name": name, "selected": True})
    save_config(cfg)
    print(f"\nДобавлено {len(found)} получателей в config.json (всего {len(cfg['recipients'])}).")


# --------------------------------------------------------------------------- #
#  Слежение за процессом LoL
# --------------------------------------------------------------------------- #
class GameWatcher(threading.Thread):
    """Ловит «нарастающий фронт»: процесс был не запущен → стал запущен → рассылка."""

    def __init__(self, cfg: dict, on_launch, on_state=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.on_launch = on_launch
        self.on_state = on_state or (lambda running: None)
        self.armed = bool(cfg.get("autostart_armed", True))
        self._stop = threading.Event()
        self._was_running = False

    def stop(self):
        self._stop.set()

    @staticmethod
    def _is_running(names) -> bool:
        import psutil
        wanted = {n.lower() for n in names}
        for p in psutil.process_iter(["name"]):
            try:
                if (p.info["name"] or "").lower() in wanted:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def run(self):
        names = self.cfg.get("watch_processes", [])
        interval = float(self.cfg.get("poll_interval_sec", 5))
        rearm = bool(self.cfg.get("rearm_after_close", True))

        while not self._stop.is_set():
            running = self._is_running(names)
            self.on_state(running)

            if running and not self._was_running:
                # игра только что запустилась
                if self.armed:
                    print("🎮 Обнаружен запуск League of Legends — рассылаю…")
                    try:
                        self.on_launch()
                    except Exception as e:
                        print(f"Ошибка рассылки: {e}")
                    if not rearm:
                        self.armed = False  # больше не слать до перезапуска приложения
            if not running and self._was_running and rearm:
                # игра закрылась — снова «взводим» для следующего запуска
                self.armed = True

            self._was_running = running
            self._stop.wait(interval)


# --------------------------------------------------------------------------- #
#  Окно настроек (tkinter)
# --------------------------------------------------------------------------- #
_settings_lock = threading.Lock()


def open_settings(cfg: dict, on_broadcast=None):
    """Окно настроек: режим игры, текст, кому слать."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("Настройки рассылки — LoL → Telegram")
    root.configure(padx=18, pady=16)
    root.resizable(False, False)

    bold = ("Segoe UI", 11, "bold")

    # --- локальные тексты режимов ---
    messages = dict(cfg.get("messages", {}))
    for key, _ in GAME_MODES:
        messages.setdefault(key, cfg.get("message", ""))

    mode_var = tk.StringVar(value=cfg.get("game_mode", "ranked"))
    current_mode = {"key": mode_var.get()}

    # === Режим игры ===
    tk.Label(root, text="Во что играем:", font=bold).grid(row=0, column=0, sticky="w")
    frm_mode = tk.Frame(root)
    frm_mode.grid(row=1, column=0, sticky="w", pady=(2, 10))

    text = tk.Text(root, width=46, height=3, wrap="word", font=("Segoe UI", 10))

    def load_mode_text():
        text.delete("1.0", "end")
        text.insert("1.0", messages.get(current_mode["key"], ""))

    def on_mode_change():
        # сохранить текущий текст в прежний режим, загрузить новый
        messages[current_mode["key"]] = text.get("1.0", "end").strip()
        current_mode["key"] = mode_var.get()
        load_mode_text()

    for key, label in GAME_MODES:
        tk.Radiobutton(frm_mode, text=label, variable=mode_var, value=key,
                       command=on_mode_change).pack(side="left", padx=(0, 10))

    # === Текст сообщения ===
    tk.Label(root, text="Текст сообщения для этого режима:", font=bold)\
        .grid(row=2, column=0, sticky="w")
    text.grid(row=3, column=0, sticky="we", pady=(2, 12))
    load_mode_text()

    # === Кому отправлять ===
    tk.Label(root, text="Кому отправлять:", font=bold).grid(row=4, column=0, sticky="w")
    send_to = tk.StringVar(value=cfg.get("send_to", "all"))

    frm_to = tk.Frame(root)
    frm_to.grid(row=5, column=0, sticky="w", pady=(2, 0))

    recips = cfg.get("recipients", [])
    check_vars = []        # (recipient_dict, BooleanVar)
    check_widgets = []

    def update_checks_state():
        state = "normal" if send_to.get() == "selected" else "disabled"
        for cb in check_widgets:
            cb.configure(state=state)

    tk.Radiobutton(frm_to, text="🦸 Avengers — общий сбор (всем)",
                   variable=send_to, value="all",
                   command=update_checks_state).pack(anchor="w")
    tk.Radiobutton(frm_to, text="Выбрать вручную:",
                   variable=send_to, value="selected",
                   command=update_checks_state).pack(anchor="w")

    frm_list = tk.Frame(root)
    frm_list.grid(row=6, column=0, sticky="w", padx=(24, 0))

    if recips:
        for r in recips:
            name = r.get("name", r.get("chat_id")) if isinstance(r, dict) else r
            v = tk.BooleanVar(value=(r.get("selected", True) if isinstance(r, dict) else True))
            cb = tk.Checkbutton(frm_list, text=str(name), variable=v)
            cb.pack(anchor="w")
            if isinstance(r, dict):
                check_vars.append((r, v))
            check_widgets.append(cb)
    else:
        tk.Label(frm_list, text="(нет получателей — собери их через --collect-ids)",
                 fg="#888").pack(anchor="w")

    update_checks_state()

    # === Кнопки ===
    def apply_to_cfg():
        messages[current_mode["key"]] = text.get("1.0", "end").strip()
        cfg["messages"] = messages
        cfg["game_mode"] = mode_var.get()
        cfg["send_to"] = send_to.get()
        for r, v in check_vars:
            r["selected"] = v.get()
        save_config(cfg)

    def on_save():
        apply_to_cfg()
        messagebox.showinfo("Сохранено", "Настройки сохранены.")

    def on_send_now():
        apply_to_cfg()
        if on_broadcast:
            threading.Thread(target=on_broadcast, daemon=True).start()
        else:
            threading.Thread(target=lambda: broadcast(cfg), daemon=True).start()
        messagebox.showinfo("Рассылка", "Рассылка запущена.")

    frm_btn = tk.Frame(root)
    frm_btn.grid(row=7, column=0, sticky="e", pady=(16, 0))
    tk.Button(frm_btn, text="Разослать сейчас", command=on_send_now)\
        .pack(side="right", padx=(8, 0))
    tk.Button(frm_btn, text="Сохранить", command=on_save).pack(side="right")

    root.mainloop()


def open_settings_async(cfg: dict, on_broadcast=None):
    """Открыть окно настроек в отдельном потоке (для вызова из трея). Один экземпляр."""
    def target():
        if not _settings_lock.acquire(blocking=False):
            return
        try:
            open_settings(cfg, on_broadcast)
        finally:
            _settings_lock.release()
    threading.Thread(target=target, daemon=True).start()


# --------------------------------------------------------------------------- #
#  Трей-приложение
# --------------------------------------------------------------------------- #
def run_tray(cfg: dict):
    import pystray
    from PIL import Image, ImageDraw

    def make_icon(color):
        img = Image.new("RGB", (64, 64), "#0f172a")
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill=color)
        return img

    icon_armed = make_icon("#38bdf8")
    icon_idle = make_icon("#64748b")

    state = {"running": False}

    def do_broadcast():
        broadcast(cfg)

    watcher = GameWatcher(cfg, on_launch=do_broadcast,
                          on_state=lambda r: state.__setitem__("running", r))

    def toggle_armed(icon, item):
        watcher.armed = not watcher.armed
        icon.icon = icon_armed if watcher.armed else icon_idle
        icon.update_menu()

    def send_now(icon, item):
        threading.Thread(target=do_broadcast, daemon=True).start()

    def settings(icon, item):
        open_settings_async(cfg, do_broadcast)

    def status_text(item):
        g = "в игре" if state["running"] else "не запущена"
        a = "вкл" if watcher.armed else "выкл"
        mode = MODE_LABELS.get(cfg.get("game_mode", "ranked"), "?")
        scope = "Avengers (всем)" if cfg.get("send_to", "all") == "all" else "выбранным"
        return f"LoL: {g} · {mode} → {scope} · авто: {a}"

    menu = pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Настройки…", settings),
        pystray.MenuItem("Рассылка активна", toggle_armed,
                         checked=lambda item: watcher.armed),
        pystray.MenuItem("Разослать сейчас", send_now),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", lambda icon, item: icon.stop()),
    )

    icon = pystray.Icon("LoL-TG", icon_armed if watcher.armed else icon_idle,
                        "LoL → Telegram", menu)

    watcher.start()
    print("Трей-приложение запущено. Свернулось в системный трей.")
    icon.run()
    watcher.stop()


# --------------------------------------------------------------------------- #
#  Точка входа
# --------------------------------------------------------------------------- #
def validate(cfg: dict):
    token = cfg.get("bot_token", "")
    if not token or "ВСТАВЬ" in token:
        raise SystemExit("Укажи bot_token в config.json (получить у @BotFather).")
    me = tg_call(token, "getMe")
    print(f"Бот: @{me.get('username')} (id={me.get('id')})")
    print(f"Получателей в списке: {len(cfg.get('recipients', []))}")


def main():
    cfg = load_config()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    if arg == "--collect-ids":
        validate(cfg)
        collect_ids(cfg)
    elif arg == "--send-now":
        validate(cfg)
        broadcast(cfg)
    elif arg == "--check":
        validate(cfg)
    elif arg == "--settings":
        open_settings(cfg)  # на главном потоке — безопасно на любой ОС
    else:
        validate(cfg)
        run_tray(cfg)


if __name__ == "__main__":
    main()
