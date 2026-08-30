"""驻留浮窗: 跟随鼠标光标的无焦点气泡.

行为:
- 显示润色文本, dwell_ms 内无操作 -> 自动提交 (返回文本)
- 点击可编辑; 编辑即暂停自动计时; Enter/Ctrl+Enter 提交; Esc 原样提交(放弃修改)
- WS_EX_NOACTIVATE + 不抢键盘焦点, 用户打字不受影响
- 任何异常由调用方 (client.dwell_and_capture) 兜底
"""

import ctypes
import threading


def dwell_overlay(text: str, dwell_ms: int = 1500) -> str:
    result = {"text": text, "done": False}
    done_evt = threading.Event()

    def run():
        import tkinter as tk
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)

        # 定位到光标附近 (右下偏移 12px, 出屏则回拉)
        pt = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        root.update_idletasks()
        w = min(520, max(240, len(text) * 12 + 40))
        h = 56
        sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
        x = min(max(8, pt.x + 12), sw - w - 8)
        y = min(max(8, pt.y + 16), sh - h - 8)
        root.geometry(f"{w}x{h}+{x}+{y}")

        var = tk.StringVar(value=text)
        entry = tk.Entry(root, textvariable=var, font=("Microsoft YaHei UI", 11),
                         justify="left", relief="solid", bd=1)
        entry.pack(fill="both", expand=True, padx=2, pady=2)
        root.configure(bg="#1e1e1e")

        def commit(_=None):
            if not result["done"]:
                result["text"] = var.get()
                result["done"] = True
            root.quit()

        def on_edit_start(_=None):
            root.after_cancel(auto_id[0])  # 用户上手 -> 取消自动提交

        auto_id = [None]
        def schedule_auto():
            auto_id[0] = root.after(dwell_ms, commit)

        entry.bind("<Return>", commit)
        entry.bind("<Escape>", lambda e: (var.set(text), commit()))
        entry.bind("<Button-1>", on_edit_start, add="+")
        entry.bind("<Key>", on_edit_start, add="+")

        # WS_EX_NOACTIVATE: 不激活不抢焦点; 立即绘制
        root.update_idletasks()
        try:
            hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

        schedule_auto()
        root.after(dwell_ms + 60_000, commit)  # 60s 兜底, 永不挂死
        root.mainloop()
        try:
            root.destroy()
        except Exception:
            pass
        done_evt.set()

    import ctypes.wintypes  # noqa: F401 (overlay 仅 Windows)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    done_evt.wait(timeout=(dwell_ms + 65_000) / 1000.0)
    return result["text"]
