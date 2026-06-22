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
import os
import json
import time
import threading
from pathlib import Path

import requests

# Папка с конфигом: рядом с .exe (frozen) или рядом со скриптом
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).parent
else:
    BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
API = "https://api.telegram.org/bot{token}/{method}"


def resource_path(name: str) -> Path:
    """Путь к ресурсу (assets/...). В .exe PyInstaller распаковывает их в _MEIPASS."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / "assets" / name

# Дефолтный конфиг — создаётся при первом запуске, если файла ещё нет
DEFAULT_CONFIG = {
    "bot_token": "",
    "proxy": "",
    "game_mode": "ranked",
    "send_to": "all",
    "messages": {
        "ranked": "🏆 AVENGERS, общий сбор! Го ранкеды в League of Legends!",
        "aram": "💫 AVENGERS, общий сбор! Го АРАМ!",
        "arena": "⚔️ AVENGERS, общий сбор! Го Арены!",
    },
    "recipients": [],
    "watch_processes": ["LeagueClient.exe", "League of Legends.exe"],
    "poll_interval_sec": 5,
    "send_delay_sec": 1.5,
    "rearm_after_close": True,
    "autostart_armed": True,
}

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
def ensure_config() -> bool:
    """Создать config.json со значениями по умолчанию, если его нет. True — если создан."""
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return True
    return False


def embedded_token() -> str:
    """Токен, вшитый при сборке .exe (модуль _embedded). Пусто, если не вшивался."""
    try:
        from _embedded import TOKEN
        return (TOKEN or "").strip()
    except Exception:
        return ""


def load_config() -> dict:
    ensure_config()
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = json.load(f)
    # Если токен вшит при сборке — он имеет приоритет (вводить вручную не нужно)
    emb = embedded_token()
    if emb:
        cfg["bot_token"] = emb
    set_proxies(cfg.get("proxy", ""))
    return cfg


# Прокси для запросов к Telegram (если api.telegram.org заблокирован)
_PROXIES = None


def set_proxies(proxy: str):
    """Задать прокси для всех запросов к Telegram. Пусто — без прокси."""
    global _PROXIES
    proxy = (proxy or "").strip()
    _PROXIES = {"http": proxy, "https": proxy} if proxy else None


def has_valid_token(cfg: dict) -> bool:
    token = cfg.get("bot_token", "")
    return bool(token) and "ВСТАВЬ" not in token


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
    r = requests.post(url, data=params, timeout=20, proxies=_PROXIES)
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


def fetch_new_recipients(cfg: dict) -> int:
    """Один запрос getUpdates: добавляет всех, кто недавно нажал Start у бота.
    Возвращает число добавленных. Используется кнопкой в окне настроек."""
    token = cfg["bot_token"]
    known = {r["chat_id"] if isinstance(r, dict) else r for r in cfg.get("recipients", [])}
    updates = tg_call(token, "getUpdates", timeout=2)
    added = 0
    cfg.setdefault("recipients", [])
    for u in updates:
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        chat = msg["chat"]
        if chat["type"] != "private":
            continue
        cid = chat["id"]
        if cid in known:
            continue
        name = " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) \
            or chat.get("username") or str(cid)
        cfg["recipients"].append({"chat_id": cid, "name": name, "selected": True})
        known.add(cid)
        added += 1
    if added:
        save_config(cfg)
    return added


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
#  Окно настроек (webview, с откатом на tkinter)
# --------------------------------------------------------------------------- #
class _JsApi:
    """Мост между HTML-окном (JS) и Python. Методы вызываются из settings.html."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._window = None

    def _state(self) -> dict:
        recips = [{"chat_id": r["chat_id"], "name": r.get("name", r["chat_id"]),
                   "selected": r.get("selected", True)}
                  for r in self.cfg.get("recipients", []) if isinstance(r, dict)]
        return {
            "game_mode": self.cfg.get("game_mode", "ranked"),
            "send_to": self.cfg.get("send_to", "all"),
            "messages": self.cfg.get("messages", {}),
            "recipients": recips,
            "token_embedded": bool(embedded_token()),
            "proxy": self.cfg.get("proxy", ""),
        }

    def _apply(self, payload: dict):
        if payload.get("bot_token") and not embedded_token():
            self.cfg["bot_token"] = payload["bot_token"].strip()
        if "proxy" in payload:
            self.cfg["proxy"] = (payload.get("proxy") or "").strip()
            set_proxies(self.cfg["proxy"])
        if payload.get("messages"):
            self.cfg["messages"] = payload["messages"]
        if payload.get("game_mode"):
            self.cfg["game_mode"] = payload["game_mode"]
        if payload.get("send_to"):
            self.cfg["send_to"] = payload["send_to"]
        selected = set(payload.get("selected", []))
        for r in self.cfg.get("recipients", []):
            if isinstance(r, dict):
                r["selected"] = r["chat_id"] in selected
        save_config(self.cfg)

    # --- методы для JS ---
    def save(self, payload):
        self._apply(payload or {})
        return {"ok": True}

    def send(self, payload):
        self._apply(payload or {})
        if not has_valid_token(self.cfg):
            return {"error": "Токен бота не задан"}
        recips = active_recipients(self.cfg)
        if not recips:
            return {"error": "Список пуст — нажми «Обновить список»"}
        token = self.cfg["bot_token"]
        try:
            tg_call(token, "getMe")
        except Exception as e:
            return {"error": f"Токен неверный ({e})"}
        text = active_message(self.cfg)
        delay = float(self.cfg.get("send_delay_sec", 1.5))
        sent, first_err = 0, None
        for r in recips:
            cid = r["chat_id"] if isinstance(r, dict) else r
            try:
                send_message(token, cid, text)
                sent += 1
            except Exception as e:
                first_err = first_err or str(e)
            time.sleep(delay)
        if sent == 0:
            return {"error": f"Не доставлено никому. {first_err or ''}".strip()}
        return {"ok": True, "sent": sent, "total": len(recips)}

    def collect(self, payload):
        payload = payload or {}
        if payload.get("bot_token") and not embedded_token():
            self.cfg["bot_token"] = payload["bot_token"].strip()
        if "proxy" in payload:
            self.cfg["proxy"] = (payload.get("proxy") or "").strip()
            set_proxies(self.cfg["proxy"])
        if not has_valid_token(self.cfg):
            return {"error": "Сначала задай токен бота"}
        try:
            added = fetch_new_recipients(self.cfg)
        except Exception as e:
            return {"error": f"Ошибка Telegram: {e}"}
        return {"added": added, "recipients": self._state()["recipients"]}

    def close(self, payload=None):
        if self._window:
            self._window.destroy()
        return {"ok": True}


def open_settings(cfg: dict, on_broadcast=None):
    """Окно настроек. Красивый webview-интерфейс; если webview недоступен — tkinter."""
    try:
        import webview  # pywebview
    except Exception:
        return open_settings_tk(cfg, on_broadcast)

    try:
        html = resource_path("settings.html").read_text(encoding="utf-8")
    except Exception:
        return open_settings_tk(cfg, on_broadcast)

    api = _JsApi(cfg)
    html = html.replace("STATE_JSON_PLACEHOLDER", json.dumps(api._state(), ensure_ascii=False))

    # HTML с котами ~2 МБ, а у WebView2 лимит на NavigateToString (html=...) ≈ 2 МБ →
    # чёрный экран. Поэтому рендерим во временный файл и грузим его по пути (без лимита).
    import tempfile
    fd, tmp_path = tempfile.mkstemp(suffix=".html", prefix="sbor_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)

    try:
        window = webview.create_window(
            "Сбор друзей — настройки",
            url=tmp_path, js_api=api, width=1100, height=900,
            maximized=True, background_color="#080d18", resizable=True,
        )
        api._window = window
        webview.start()
    except Exception as e:
        # нет WebView2 / бэкенд не стартовал — откат на tkinter
        print(f"webview не запустился ({e}) — открываю запасное окно.")
        return open_settings_tk(cfg, on_broadcast)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def open_settings_tk(cfg: dict, on_broadcast=None):
    """Запасное окно настроек на tkinter (если webview недоступен)."""
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.title("😺 Настройки — LoL → Telegram")
    root.resizable(False, False)

    # --- кото-фон + иконка окна ---
    try:
        bg_photo = tk.PhotoImage(file=str(resource_path("cat_bg.png")))
        W, H = bg_photo.width(), bg_photo.height()
    except Exception:
        bg_photo, W, H = None, 600, 820
    try:
        icon_photo = tk.PhotoImage(file=str(resource_path("cat_icon.png")))
        root.iconphoto(True, icon_photo)
    except Exception:
        icon_photo = None
    root._imgs = [bg_photo, icon_photo]  # защита от сборщика мусора

    canvas = tk.Canvas(root, width=W, height=H, highlightthickness=0, bg="#0f121e")
    canvas.pack()
    if bg_photo:
        canvas.create_image(0, 0, anchor="nw", image=bg_photo)
    canvas.create_text(W // 2 + 1, 35, text="😺 Avengers — кото-сбор 🐾",
                       font=("Segoe UI", 17, "bold"), fill="#000000")
    canvas.create_text(W // 2, 34, text="😺 Avengers — кото-сбор 🐾",
                       font=("Segoe UI", 17, "bold"), fill="#ffd166")

    # карточка с контролами поверх котиков
    card = tk.Frame(canvas, padx=18, pady=14, bg="#f6f5f2",
                    relief="ridge", bd=2)
    canvas.create_window(W // 2, 64, anchor="n", window=card)

    bold = ("Segoe UI", 11, "bold")
    CARD = "#f6f5f2"
    r = 0  # счётчик строк grid

    # === Токен бота ===
    tk.Label(card, text="🐾 Токен бота (от @BotFather):", font=bold, bg=CARD)\
        .grid(row=r, column=0, sticky="w"); r += 1
    token_var = tk.StringVar(value=cfg.get("bot_token", ""))
    token_entry = tk.Entry(card, textvariable=token_var, width=48, show="•")
    token_entry.grid(row=r, column=0, sticky="we", pady=(2, 12)); r += 1

    # --- локальные тексты режимов ---
    messages = dict(cfg.get("messages", {}))
    for key, _ in GAME_MODES:
        messages.setdefault(key, cfg.get("message", ""))

    mode_var = tk.StringVar(value=cfg.get("game_mode", "ranked"))
    current_mode = {"key": mode_var.get()}

    # === Режим игры ===
    tk.Label(card, text="🎮 Во что играем:", font=bold, bg=CARD)\
        .grid(row=r, column=0, sticky="w"); r += 1
    frm_mode = tk.Frame(card, bg=CARD)
    frm_mode.grid(row=r, column=0, sticky="w", pady=(2, 10)); r += 1

    text = tk.Text(card, width=46, height=3, wrap="word", font=("Segoe UI", 10))

    def load_mode_text():
        text.delete("1.0", "end")
        text.insert("1.0", messages.get(current_mode["key"], ""))

    def on_mode_change():
        messages[current_mode["key"]] = text.get("1.0", "end").strip()
        current_mode["key"] = mode_var.get()
        load_mode_text()

    for key, label in GAME_MODES:
        tk.Radiobutton(frm_mode, text=label, variable=mode_var, value=key,
                       command=on_mode_change, bg=CARD,
                       activebackground=CARD).pack(side="left", padx=(0, 10))

    # === Текст сообщения ===
    tk.Label(card, text="✉️ Текст сообщения для этого режима:", font=bold, bg=CARD)\
        .grid(row=r, column=0, sticky="w"); r += 1
    text.grid(row=r, column=0, sticky="we", pady=(2, 12)); r += 1
    load_mode_text()

    # === Кому отправлять ===
    tk.Label(card, text="🐱 Кому отправлять:", font=bold, bg=CARD)\
        .grid(row=r, column=0, sticky="w"); r += 1
    send_to = tk.StringVar(value=cfg.get("send_to", "all"))

    frm_to = tk.Frame(card, bg=CARD)
    frm_to.grid(row=r, column=0, sticky="w", pady=(2, 0)); r += 1

    check_vars = []        # (recipient_dict, BooleanVar)
    check_widgets = []

    def update_checks_state():
        state = "normal" if send_to.get() == "selected" else "disabled"
        for cb in check_widgets:
            cb.configure(state=state)

    tk.Radiobutton(frm_to, text="🦸 Avengers — общий сбор (всем)",
                   variable=send_to, value="all", bg=CARD, activebackground=CARD,
                   command=update_checks_state).pack(anchor="w")
    tk.Radiobutton(frm_to, text="Выбрать вручную:",
                   variable=send_to, value="selected", bg=CARD, activebackground=CARD,
                   command=update_checks_state).pack(anchor="w")

    frm_list = tk.Frame(card, bg=CARD)
    frm_list.grid(row=r, column=0, sticky="w", padx=(24, 0)); r += 1

    def rebuild_recipient_list():
        for w in frm_list.winfo_children():
            w.destroy()
        check_vars.clear()
        check_widgets.clear()
        recips = cfg.get("recipients", [])
        if recips:
            for rec in recips:
                if not isinstance(rec, dict):
                    continue
                v = tk.BooleanVar(value=rec.get("selected", True))
                cb = tk.Checkbutton(frm_list, text="🐈 " + str(rec.get("name", rec.get("chat_id"))),
                                    variable=v, bg=CARD, activebackground=CARD)
                cb.pack(anchor="w")
                check_vars.append((rec, v))
                check_widgets.append(cb)
        else:
            tk.Label(frm_list, text="(пока пусто — нажми «Собрать получателей»)",
                     fg="#888", bg=CARD).pack(anchor="w")
        update_checks_state()

    rebuild_recipient_list()

    # === Сбор получателей ===
    collect_status = tk.Label(card, text="", fg="#2563eb", bg=CARD)
    collect_status.grid(row=r, column=0, sticky="w", pady=(6, 0)); r += 1

    def on_collect():
        cfg["bot_token"] = token_var.get().strip()
        if not has_valid_token(cfg):
            messagebox.showwarning("Нет токена", "Сначала вставь токен бота.")
            return
        collect_status.config(text="Опрашиваю Telegram…")
        root.update_idletasks()
        try:
            added = fetch_new_recipients(cfg)
        except Exception as e:
            collect_status.config(text=f"Ошибка: {e}", fg="#dc2626")
            return
        rebuild_recipient_list()
        total = len(cfg.get("recipients", []))
        collect_status.config(
            text=f"Добавлено новых: {added}. Всего получателей: {total}.", fg="#16a34a")

    # === Кнопки ===
    def apply_to_cfg():
        messages[current_mode["key"]] = text.get("1.0", "end").strip()
        cfg["bot_token"] = token_var.get().strip()
        cfg["messages"] = messages
        cfg["game_mode"] = mode_var.get()
        cfg["send_to"] = send_to.get()
        for rec, v in check_vars:
            rec["selected"] = v.get()
        save_config(cfg)

    def on_save():
        apply_to_cfg()
        messagebox.showinfo("Сохранено", "Настройки сохранены.")

    def on_send_now():
        apply_to_cfg()
        if not has_valid_token(cfg):
            messagebox.showwarning("Нет токена", "Сначала вставь токен бота.")
            return
        target = on_broadcast or (lambda: broadcast(cfg))
        threading.Thread(target=target, daemon=True).start()
        messagebox.showinfo("Рассылка", "Рассылка запущена.")

    frm_btn = tk.Frame(card, bg=CARD)
    frm_btn.grid(row=r, column=0, sticky="we", pady=(16, 0))
    tk.Button(frm_btn, text="🐾 Собрать получателей", command=on_collect)\
        .pack(side="left")
    tk.Button(frm_btn, text="📨 Разослать сейчас", command=on_send_now)\
        .pack(side="right", padx=(8, 0))
    tk.Button(frm_btn, text="💾 Сохранить", command=on_save).pack(side="right")

    root.mainloop()


def open_settings_process():
    """Открыть окно настроек ОТДЕЛЬНЫМ процессом — webview требует своего главного потока."""
    import subprocess
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--settings"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve()), "--settings"]
    subprocess.Popen(cmd, close_fds=True)


# --------------------------------------------------------------------------- #
#  Трей-приложение
# --------------------------------------------------------------------------- #
def run_tray(cfg: dict):
    import pystray
    from PIL import Image, ImageDraw, ImageEnhance

    def fallback_icon(color):
        img = Image.new("RGB", (64, 64), "#0f172a")
        d = ImageDraw.Draw(img)
        d.ellipse((16, 16, 48, 48), fill=color)
        return img

    try:
        cat = Image.open(resource_path("cat_icon.png")).convert("RGB")
        icon_armed = cat                                   # активна — яркий кот
        icon_idle = ImageEnhance.Color(cat).enhance(0.2)   # выкл — приглушённый кот
    except Exception:
        icon_armed = fallback_icon("#38bdf8")
        icon_idle = fallback_icon("#64748b")

    state = {"running": False}

    def do_broadcast():
        # перечитываем конфиг с диска — окно настроек (отдельный процесс) мог его изменить
        broadcast(load_config())

    watcher = GameWatcher(cfg, on_launch=do_broadcast,
                          on_state=lambda r: state.__setitem__("running", r))

    def toggle_armed(icon, item):
        watcher.armed = not watcher.armed
        icon.icon = icon_armed if watcher.armed else icon_idle
        icon.update_menu()

    def send_now(icon, item):
        threading.Thread(target=do_broadcast, daemon=True).start()

    def settings(icon, item):
        open_settings_process()

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
    ensure_config()
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
        # При запуске всегда показываем окно настроек, потом уходим в трей
        open_settings(cfg)
        cfg = load_config()
        if not has_valid_token(cfg):
            print("Токен не задан. Выход.")
            return
        run_tray(cfg)


if __name__ == "__main__":
    main()
