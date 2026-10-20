"""Regression: a slow Games scan must never block the Start button or UI loop."""
import ctypes, json, sys, tempfile, threading, time, tkinter as tk
from pathlib import Path
from unittest.mock import patch
ctypes.windll.shcore.SetProcessDpiAwareness(1)
sys.path.insert(0,str(Path(__file__).resolve().parent))
from app import App, Settings
from engine import Engine
from runtime import find_7zip
from test_converter import TestDrive,make_iso
from core import Cancelled
results={}
def pump(root,predicate=lambda:False,seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        root.update();time.sleep(.025)
        if predicate():return True
    return predicate()
def main():
    with tempfile.TemporaryDirectory(prefix='xbox-v21-regression-') as tmp:
        base=Path(tmp).resolve();drive=TestDrive(base/'usb');e=Engine(base/'input',find_7zip(),drive,settle_seconds=0)
        iso=e.base/'Start button test.iso';make_iso(iso);e.delete_sources=False
        root=tk.Tk();app=App(root,e);release=threading.Event();entered=threading.Event()
        def slow_scan(*args):
            entered.set();release.wait(10)
            return []
        try:
            e.scan();e.scan();pump(root,lambda:app.last_snapshot and app.last_snapshot['drive'])
            with patch('app.scan_library',slow_scan):
                app.tabs.select(app.games_tab);assert pump(root,entered.is_set)
                app.tabs.select(app.queue_tab);pump(root,seconds=.5)
                results['scan_does_not_block_start']=str(app.start['state'])=='normal' and not app.busy and not e.maintenance
                assert results['scan_does_not_block_start']
                app.start.invoke()
                assert pump(root,lambda: e.jobs[0]['state']=='Complete')
                results['start_converts_while_library_is_still_scanning']=app.library_loading and e.jobs[0]['state']=='Complete'
                assert results['start_converts_while_library_is_still_scanning']
                results['start_requests_scan_cancellation']=app.library_cancel.is_set()
                release.set();assert pump(root,lambda:not app.library_loading)
            e.pause();pump(root,seconds=.5)
            failed=dict(e.jobs[0],id='a'*32,state='Error',hidden=False);e.jobs.append(failed)
            pump(root,lambda:app.tree.exists(failed['id']))
            app.clear_button.invoke();assert pump(root,lambda:not app.tree.get_children())
            results['clear_hides_completed_and_errors_but_keeps_history']=len(app.history.get_children())==2 and iso.exists()
            def fail():raise ValueError('fixture operation error')
            with patch('app.messagebox.showerror') as dialog:
                app.background(fail)
                assert pump(root,lambda:not app.busy)
                results['operation_error_releases_start']=not e.maintenance and str(app.start['state'])=='normal'
            def bad_callback(_):raise ValueError('fixture callback error')
            app.background(lambda:None,bad_callback)
            assert pump(root,lambda:not app.busy)
            pump(root,seconds=.4)
            results['callback_error_does_not_stop_ui_updates']=app.root.winfo_exists() and str(app.start['state'])=='normal'
            with patch('app.scan_library',side_effect=OSError('USB fixture disconnected')):
                app.refresh_library();assert pump(root,lambda:not app.library_loading)
                results['scan_error_leaves_start_enabled']=str(app.start['state'])=='normal' and not e.maintenance
            def within(widget,window):
                return bool(widget.winfo_ismapped() and widget.winfo_rootx()>=window.winfo_rootx() and widget.winfo_rootx()+widget.winfo_width()<=window.winfo_rootx()+window.winfo_width()+2 and widget.winfo_rooty()+widget.winfo_height()<=window.winfo_rooty()+window.winfo_height()+2)
            root.geometry('1020x780');pump(root,seconds=.2)
            controls=[app.start,app.batch_combo,app.tree,app.log,app.bar,app.clear_button]
            results['minimum_window_controls_fit']=all(within(w,root) for w in controls)
            print('Window',root.geometry(),'Clipped',[(str(w),w.winfo_class(),w.winfo_rootx()-root.winfo_rootx(),w.winfo_rooty()-root.winfo_rooty(),w.winfo_width(),w.winfo_height()) for w in controls if not within(w,root)])
            results['queue_inner_padding']=app.tree.winfo_rootx()-app.queue_tab.winfo_rootx()>=16
            results['cleanup_option_shows_real_choice']=app.batch_var.get() in ('Keep source files','Delete source files','Ask before deleting')
            settings=Settings(app);root.update()
            all_controls=[]
            def visit(w):
                for c in w.winfo_children():
                    if c.winfo_class() in ('TButton','TCombobox','TEntry','TCheckbutton'):all_controls.append(c)
                    visit(c)
            visit(settings.win)
            results['settings_controls_fit']=all(within(w,settings.win) for w in all_controls)
            print('Settings',settings.win.geometry(),'Clipped',[(str(w),w.winfo_class(),w.winfo_rootx()-settings.win.winfo_rootx(),w.winfo_rooty()-settings.win.winfo_rooty(),w.winfo_width(),w.winfo_height()) for w in all_controls if not within(w,settings.win)])
            settings.win.destroy()
            assert all(results.values()),results
        finally:
            release.set();app.cancel_library();e.close()
            for t in e.threads:t.join(timeout=5)
            if app.library_thread:app.library_thread.join(timeout=2)
            root.destroy()
            for h in e.logger.handlers:h.close()
    print(json.dumps(results,indent=2))

if __name__ == "__main__":
    main()
