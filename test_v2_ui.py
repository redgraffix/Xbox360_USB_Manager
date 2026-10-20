import ctypes, json, sys, tempfile, time, tkinter as tk
from pathlib import Path
ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.path.insert(0,str(Path(__file__).resolve().parent))
from app import App, Settings
from engine import Engine
from runtime import find_7zip
from test_converter import TestDrive,make_iso
from library import scan_library
results={}
def main():
    with tempfile.TemporaryDirectory(prefix='xbox-v2-ui-') as tmp:
        base=Path(tmp);drive=TestDrive(base/'usb');e=Engine(base/'input',find_7zip(),drive,settle_seconds=0)
        iso=e.base/'UI game.iso';make_iso(iso);e.delete_sources=False
        root=tk.Tk();app=App(root,e)
        try:
            e.scan();e.scan();e.enable();deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                root.update();time.sleep(.05)
                if e.jobs and e.jobs[0]['state']=='Complete':break
            e.pause()
            for _ in range(15):root.update();time.sleep(.05)
            assert e.jobs[0]['state']=='Complete'
            results['title']=root.title();results['queue_complete']=True
            def visible(widget,window):return bool(widget.winfo_ismapped() and widget.winfo_rooty()+widget.winfo_height()<=window.winfo_rooty()+window.winfo_height()+2)
            results['default_layout']=all(visible(w,root) for w in [app.tree,app.log,app.bar])
            root.geometry('1020x780')
            for _ in range(5):root.update()
            results['minimum_layout']=all(visible(w,root) for w in [app.tree,app.log,app.bar])
            app.clear_finished()
            for _ in range(15):root.update();time.sleep(.05)
            results['clear_keeps_history']=not app.tree.get_children() and len(app.history.get_children())==1
            app.library=scan_library(drive.root);app.library_loaded=True;app.tabs.select(app.games_tab);app.render_library();root.update()
            results['library_rows']=len(app.games_tree.get_children());app.search.set('does not exist');root.update();results['search_filters']=not app.games_tree.get_children()
            settings=Settings(app);root.update()
            results['settings_geometry']=settings.win.geometry()
            widgets=[]
            def walk(w):
                for child in w.winfo_children():widgets.append(child);walk(child)
            walk(settings.win)
            results['settings_all_controls_fit']=all(visible(w,settings.win) for w in widgets if w.winfo_class() in ('TButton','TCombobox','TEntry','TCheckbutton'))
            settings.win.destroy()
            assert all(results[k] for k in ['default_layout','minimum_layout','clear_keeps_history','search_filters','settings_all_controls_fit']),results
        finally:
            e.close()
            for t in e.threads:t.join(timeout=5)
            root.destroy()
            for h in e.logger.handlers:h.close()
    print(json.dumps(results,indent=2))

if __name__ == "__main__":
    main()
