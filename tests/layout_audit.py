"""Layout audit: report every mapped widget that is clipped by its parent or by the window edge."""
import sys, time, tkinter as tk
from pathlib import Path
S = Path(sys.argv[1])
from scrubdicom.app.ui import App
from scrubdicom.app.viewer import ViewerWindow, AppPicker
from scrubdicom.app.profile_ui import ProfileEditor
from scrubdicom.app.model import Settings
from scrubdicom.app import model

def clipped(top):
    out = []
    tx, ty, tw, th = top.winfo_rootx(), top.winfo_rooty(), top.winfo_width(), top.winfo_height()
    def walk(w):
        for c in w.winfo_children():
            if not c.winfo_ismapped() or c.winfo_class() in ("Menu",):
                continue
            x, y, cw, ch = c.winfo_rootx(), c.winfo_rooty(), c.winfo_width(), c.winfo_height()
            txt = ""
            try: txt = str(c.cget("text"))[:34]
            except tk.TclError: pass
            over_win = x + cw > tx + tw + 1 or y + ch > ty + th + 1 or x < tx - 1 or y < ty - 1
            over_par = False
            px = py = pw = ph = 0
            a = c.master
            while a is not None and a is not top:
                if a.winfo_class() in ("Frame", "TFrame", "Labelframe", "TLabelframe", "TNotebook", "TPanedwindow", "Canvas"):
                    ax, ay, aw, ah = a.winfo_rootx(), a.winfo_rooty(), a.winfo_width(), a.winfo_height()
                    if a.winfo_class() != "TPanedwindow" and (x + cw > ax + aw + 1 or y + ch > ay + ah + 1):
                        over_par = True; px, py, pw, ph = ax, ay, aw, ah; break
                a = a.master
            if (over_win or over_par) and cw > 1 and ch > 1 and c.winfo_class() not in ("Frame", "TFrame", "Canvas", "Labelframe", "TLabelframe", "TNotebook", "TPanedwindow", "Text", "Treeview", "Listbox"):
                out.append(f"  {c.winfo_class():14s} '{txt}' right={x+cw-tx} bottom={y+ch-ty} (window {tw}x{th}; parent ends {px+pw-tx},{py+ph-ty}) {'WINDOW' if over_win else 'PARENT'}")
            walk(c)
    walk(top)
    return out

app = App(offer_demo=False); app.settings = Settings.load(S / "settings_shot.json")
app.geometry("1000x760+30+30"); app.update()
report = []
# main window: every tab, every step, every mode, with "more" on and off
for tab, name in ((app.tab_home, "home"), (app.tab_series, "series"), (app.tab_verify, "verify"), (app.tab_share, "share"), (app.tab_help, "help")):
    app.nb.select(tab); app.update(); r = clipped(app)
    if r: report.append(f"[main/{name}]"); report += r
app.nb.select(app.tab_run)
for mode in model.MODES:
    for more in (False, True):
        app.v_mode.set(mode); app.v_more.set(more); app._apply_mode()
        for step in range(3):
            app._show_step(step); app.update(); r = clipped(app)
            if r: report.append(f"[main/run mode={mode} more={more} step={step+1}]"); report += r
# with data: output set so cards/tables fill
app.v_mode.set("manifest"); app._apply_mode(); app.v_manifest.set(str(S / "manifest.csv")); app.v_output.set(str(S / "out1")); app.update(); app._refresh_all()
for tab, name in ((app.tab_series, "series+data"), (app.tab_verify, "verify+data"), (app.tab_share, "share+data")):
    app.nb.select(tab); app.update(); r = clipped(app)
    if r: report.append(f"[main/{name}]"); report += r
# viewer, both modes, at its minimum size, with series loaded
for mode in ("source", "output"):
    w = ViewerWindow(app, mode, "CBB0401"); w.geometry("1280x740+30+30")
    t0 = time.time()
    while len(w.series) < 1 and time.time() - t0 < 6: w.update(); time.sleep(0.05)
    w.tv.selection_set("0" if w.series else ()); w.update(); time.sleep(0.3); w.update()
    r = clipped(w)
    if r: report.append(f"[viewer/{mode}]"); report += r
    w.destroy()
# profile editor and app picker at their minimum sizes
ed = ProfileEditor(app, ""); ed.geometry("900x560+30+30"); app.update(); r = clipped(ed)
if r: report.append("[profile editor]"); report += r
ed.destroy()
pk = AppPicker(app, on_choose=lambda p: None); pk.geometry("460x520+30+30"); app.update(); r = clipped(pk)
if r: report.append("[app picker]"); report += r
pk.destroy()
print("\n".join(report) if report else "no clipped widgets")
app.destroy()
