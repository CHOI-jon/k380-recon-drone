# AI 정찰 드론 대시보드 실행기 — 더블클릭으로 모드 선택 → 지상국 실행 → 브라우저 자동 열기
import json, os, socket, subprocess, sys, threading, time, webbrowser
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
GS = os.path.join(HERE, "ground_station.py")
CFG = os.path.join(HERE, "launcher_settings.json")
URL_PORT = 5000

MODES = [
    ("auto", "자동 (권장) — 433MHz 동글이 있으면 실비행, 없으면 파이로만"),
    ("sim", "시뮬레이션 (드론 없이 · 매빅 영상 리플레이)"),
    ("real", "실비행 (433MHz 텔레메트리 + 파이)"),
    ("pi", "파이로만 (텔레메트리도 파이 경유)"),
    ("bench", "실내 지상 시험 (가상 고도 10 m)"),
]


def python_exe():
    exe = sys.executable
    d, b = os.path.split(exe)
    if b.lower() == "pythonw.exe" and os.path.exists(os.path.join(d, "python.exe")):
        return os.path.join(d, "python.exe")
    return exe


TELEM_VIDS = {0x10C4, 0x0403, 0x1A86, 0x067B}  # SiK 동글에 쓰이는 USB 시리얼 칩 (CP210x, FTDI, CH340, PL2303)


def com_ports():
    try:
        from serial.tools import list_ports
        return [f"{p.device} - {p.description}" for p in list_ports.comports()]
    except Exception:
        return []


def find_telem_port():
    """433MHz 텔레메트리 동글로 보이는 COM 포트. 블루투스 가상 포트는 제외. 없으면 None."""
    try:
        from serial.tools import list_ports
        ports = list(list_ports.comports())
    except Exception:
        return None
    cand = []
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''} {p.hwid}".lower()
        if "bluetooth" in desc or "bthenum" in desc:
            continue
        score = 2 if (p.vid in TELEM_VIDS) else (1 if "usb" in desc else 0)
        if score:
            cand.append((score, p))
    if not cand:
        return None
    cand.sort(key=lambda x: -x[0])
    p = cand[0][1]
    return f"{p.device} - {p.description}"


def is_bluetooth(label):
    return "bluetooth" in (label or "").lower()


def pi_check(host, port=8765):
    """파이 상태: ok / no_detector(파이는 켜졌는데 탐지 프로그램 꺼짐) / not_found"""
    try:
        socket.getaddrinfo(host, port)
    except OSError:
        return "not_found"
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return "ok"
    except ConnectionRefusedError:
        return "no_detector"
    except OSError:
        return "not_found"


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.stopping = False
        root.title("AI 정찰 드론 대시보드 실행기")
        root.geometry("620x560")
        root.minsize(560, 480)
        cfg = self.load()

        f = ttk.Frame(root, padding=14)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="AI 정찰 드론 대시보드", font=("맑은 고딕", 15, "bold")).pack(anchor="w")
        ttk.Label(f, text="모드를 고르고 [시작]을 누르면 지상국이 켜지고 대시보드가 자동으로 열립니다.",
                  foreground="#555").pack(anchor="w", pady=(2, 10))

        box = ttk.LabelFrame(f, text="모드", padding=8)
        box.pack(fill="x")
        # 처음 한 번은 자동 모드로 전환 (이전 설정 파일에는 실비행+블루투스 포트가 저장돼 있었음)
        self.mode = tk.StringVar(value=cfg.get("mode", "auto") if cfg.get("v", 1) >= 2 else "auto")
        for key, label in MODES:
            ttk.Radiobutton(box, text=label, value=key, variable=self.mode,
                            command=self.update_fields).pack(anchor="w", pady=1)

        opt = ttk.LabelFrame(f, text="연결 설정", padding=8)
        opt.pack(fill="x", pady=8)
        ttk.Label(opt, text="텔레메트리 COM 포트").grid(row=0, column=0, sticky="w")
        self.com = ttk.Combobox(opt, width=34, values=com_ports())
        saved = cfg.get("com", "")
        if not saved or is_bluetooth(saved) or saved.split(" - ")[0] not in [x.split(" - ")[0] for x in com_ports()]:
            saved = find_telem_port() or saved
        self.com.set(saved)
        self.com.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Button(opt, text="새로고침", command=self.refresh_ports).grid(row=0, column=2)
        ttk.Label(opt, text="파이 주소").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.pi = ttk.Entry(opt, width=36)
        self.pi.insert(0, cfg.get("pi", "ra4.local"))
        self.pi.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(opt, text="(ra4.local 이 안 되면 파이 IP)", foreground="#777").grid(row=1, column=2, sticky="w", pady=(6, 0))
        self.auto_open = tk.BooleanVar(value=cfg.get("auto_open", True))
        ttk.Checkbutton(opt, text="시작하면 브라우저로 대시보드 자동 열기",
                        variable=self.auto_open).grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=4)
        self.b_start = ttk.Button(btns, text="▶ 시작", command=self.start)
        self.b_start.pack(side="left")
        self.b_stop = ttk.Button(btns, text="■ 정지", command=self.stop, state="disabled")
        self.b_stop.pack(side="left", padx=6)
        ttk.Button(btns, text="대시보드 열기", command=self.open_browser).pack(side="left")
        self.status = ttk.Label(btns, text="대기 중", foreground="#555")
        self.status.pack(side="right")

        ttk.Label(f, text="실행 기록").pack(anchor="w", pady=(6, 0))
        lf = ttk.Frame(f)
        lf.pack(fill="both", expand=True)
        self.log = tk.Text(lf, height=12, font=("Consolas", 9), wrap="word", state="disabled")
        sb = ttk.Scrollbar(lf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.update_fields()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if port_open(URL_PORT):
            self.write("이미 대시보드가 실행 중입니다 (포트 5000). [대시보드 열기]로 바로 볼 수 있습니다.\n")

    # ---------- 설정 ----------
    def load(self):
        try:
            with open(CFG, encoding="utf-8") as fp:
                return json.load(fp)
        except Exception:
            return {}

    def save(self):
        try:
            with open(CFG, "w", encoding="utf-8") as fp:
                json.dump(dict(v=2, mode=self.mode.get(), com=self.com.get(), pi=self.pi.get(),
                               auto_open=self.auto_open.get()), fp, ensure_ascii=False)
        except Exception:
            pass

    def refresh_ports(self):
        ports = com_ports()
        self.com["values"] = ports
        t = find_telem_port()
        if t:
            self.com.set(t)
            self.write(f"433MHz 텔레메트리 동글: {t}\n")
        else:
            self.write("433MHz 텔레메트리 동글을 찾지 못했습니다 (블루투스 포트는 제외). "
                       "동글을 꽂았으면 다시 [새로고침], 없으면 '자동'이나 '파이로만' 모드로 시작하세요.\n")

    def update_fields(self):
        m = self.mode.get()
        self.com.configure(state="normal" if m in ("real", "auto") else "disabled")
        self.pi.configure(state="disabled" if m == "sim" else "normal")

    # ---------- 실행 ----------
    def args(self):
        m = self.mode.get()
        a = [python_exe(), "-u", GS]
        if m == "auto":
            t = find_telem_port()
            if t:
                self.com.set(t)
                m = "real"
                self.write(f"[자동] 433MHz 동글 {t.split(' - ')[0]} 발견 → 실비행 모드\n")
            else:
                m = "pi"
                self.write("[자동] 433MHz 동글 없음 → 파이로만 모드 (텔레메트리도 파이 경유)\n")
        if m == "sim":
            a += ["--sim-telem", "--sim-pi"]
        elif m == "real":
            if is_bluetooth(self.com.get()):
                t = find_telem_port()
                if not t:
                    raise ValueError("고른 포트는 블루투스 포트입니다. 433MHz 동글을 꽂고 [새로고침] 하거나 '자동'/'파이로만' 모드를 쓰세요.")
                self.com.set(t)
            com = self.com.get().split(" - ")[0].strip()
            if not com:
                raise ValueError("COM 포트를 고르세요.")
            a += ["--com", com, "--baud", "57600", "--pi", self.pi.get().strip()]
        elif m == "pi":
            a += ["--pi", self.pi.get().strip(), "--telem-from-pi"]
        elif m == "bench":
            a += ["--pi", self.pi.get().strip(), "--telem-from-pi", "--bench-alt", "10",
                  "--home-fallback", "--home", "37.8700,127.7410"]
        return a

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        if port_open(URL_PORT):
            if messagebox.askyesno("이미 실행 중", "대시보드(포트 5000)가 이미 켜져 있습니다.\n그 대시보드를 브라우저로 열까요?"):
                self.open_browser()
            return
        try:
            a = self.args()
        except ValueError as e:
            messagebox.showwarning("설정 확인", str(e))
            return
        self.save()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.write("\n$ " + " ".join(os.path.basename(x) if i < 3 else x for i, x in enumerate(a)) + "\n")
        try:
            self.proc = subprocess.Popen(a, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, env=env, creationflags=flags)
        except Exception as e:
            messagebox.showerror("실행 실패", f"파이썬 실행에 실패했습니다.\n{e}")
            return
        self.stopping = False
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        self.set_status("시작 중…", "#b45309")
        threading.Thread(target=self.reader, daemon=True).start()
        threading.Thread(target=self.wait_ready, daemon=True).start()
        if self.mode.get() != "sim":
            threading.Thread(target=self.watch_pi, args=(self.pi.get().strip(),), daemon=True).start()

    def watch_pi(self, host):
        """파이가 부팅·연결될 때까지 상태를 알기 쉽게 안내 (지상국은 알아서 계속 재연결함)"""
        last = None
        for _ in range(180):
            p = self.proc
            if not p or p.poll() is not None:
                return
            st = pi_check(host)
            if st != last:
                msg = {"ok": "[파이] 연결됨 — 영상·탐지 수신 시작",
                       "no_detector": "[파이] 파이는 켜져 있지만 탐지 프로그램이 아직 안 켜짐 (부팅 직후면 30초 정도 기다리세요. "
                                      "계속 이러면 파이 자동 실행 설정이 안 된 것: bash ~/pi/install_autostart.sh)",
                       "not_found": "[파이] 파이를 찾는 중 — 드론 배터리를 연결했는지, 파이와 노트북이 같은 핫스팟인지 확인 (부팅에 약 40초)"}[st]
                self.root.after(0, self.write, msg + "\n")
                last = st
            if st == "ok":
                return
            time.sleep(3)

    def reader(self):
        p = self.proc
        for raw in iter(p.stdout.readline, b""):
            self.root.after(0, self.write, raw.decode("utf-8", "replace"))
        code = p.wait()
        self.root.after(0, self.ended, code)

    def wait_ready(self):
        p = self.proc
        for _ in range(120):
            if p.poll() is not None:
                return
            if port_open(URL_PORT):
                self.root.after(0, self.set_status, "실행 중 · http://localhost:5000", "#15803d")
                if self.auto_open.get():
                    self.root.after(0, self.open_browser)
                return
            time.sleep(0.5)
        self.root.after(0, self.set_status, "응답 없음 — 실행 기록 확인", "#b91c1c")

    def ended(self, code):
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        if self.stopping:
            self.set_status("정지됨", "#555")
        else:
            self.set_status("지상국이 스스로 종료됨 — 아래 실행 기록 확인", "#b91c1c")
        self.write(f"[지상국 종료, 코드 {code}]\n")

    def stop(self):
        p = self.proc
        if p and p.poll() is None:
            self.stopping = True
            self.set_status("정지 중…", "#b45309")
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def open_browser(self):
        webbrowser.open(f"http://localhost:{URL_PORT}")

    # ---------- 화면 ----------
    def write(self, s):
        self.log.configure(state="normal")
        self.log.insert("end", s)
        if int(self.log.index("end-1c").split(".")[0]) > 2000:
            self.log.delete("1.0", "500.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, s, color):
        self.status.configure(text=s, foreground=color)

    def on_close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("종료", "지상국을 끄고 실행기를 닫을까요?\n(비행 중이면 '아니요')"):
                return
            self.stop()
        self.save()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except Exception:
        pass
    App(root)
    root.mainloop()
