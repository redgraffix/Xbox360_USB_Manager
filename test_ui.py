"""Test this application's own Tk widgets and worker callbacks on tiny local fixtures."""
import json, sys, tempfile, time, tkinter as tk, ctypes
ctypes.windll.shcore.SetProcessDpiAwareness(1)
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from app import App
from engine import Engine
from runtime import find_7zip
from test_converter import make_iso, TestDrive

def main():
    with tempfile.TemporaryDirectory(prefix='xbox-ui-test-') as directory:
        rootpath=Path(directory).resolve(); inputpath=rootpath/'input'; drive=TestDrive(rootpath/'test-usb')
        engine=Engine(inputpath,find_7zip(),drive,settle_seconds=0)
        make_iso(inputpath/'Interface Test (USA).iso')
        window=tk.Tk(); app=App(window,engine); window.title('Converter interface test')
        results={}
        try:
            window.update()
            results['screen']=[window.winfo_screenwidth(),window.winfo_screenheight()]
            results['geometry']=window.geometry()
            results['footer_visible']=app.footer_var.get() is not None
            # Exercise actual worker transitions, not the user's installed app or drive.
            engine.scan(); engine.scan(); engine.enable()
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                window.update(); time.sleep(.05)
                if engine.jobs and engine.jobs[0]['state']=='Complete': break
            for _ in range(10): window.update(); time.sleep(.05)
            results['state']=engine.jobs[0]['state']
            results['row_values']=list(app.tree.item(engine.jobs[0]['id'],'values'))
            results['footer']=app.footer_var.get()
            results['all_widgets_inside_window']=all(
                w.winfo_ismapped() and w.winfo_rooty()+w.winfo_height()<=window.winfo_rooty()+window.winfo_height()+2
                for w in [app.tree,app.log,app.bar])
            assert results['state']=='Complete',results
            assert results['row_values'][2]=='Complete',results
            assert results['all_widgets_inside_window'],results
            engine.pause()
            engine.options(auto=False)
            make_iso(inputpath/'Another Test.iso',{'default.xex':b'XEX2-another','level.bin':b'another level'})
            engine.scan(); engine.scan(); engine.enable()
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                window.update(); time.sleep(.05)
                if all(j['state']=='Complete' for j in engine.jobs): break
            assert all(j['state']=='Complete' for j in engine.jobs)
            # New input while auto-queue is off remains Ready, and the Start control stays usable.
            make_iso(inputpath/'Third Test.iso',{'default.xex':b'XEX2-third'})
            engine.scan(); engine.scan()
            for _ in range(10): window.update(); time.sleep(.05)
            results['ready_button_enabled']=str(app.start['state'])=='normal'
            results['ready_button_text']=app.start['text']
            assert results['ready_button_enabled'],results
            assert str(app.start['text'])=='Start queue',results
        finally:
            engine.close()
            for thread in engine.threads: thread.join(timeout=5)
            window.destroy()
            for h in engine.logger.handlers: h.close()
        print(json.dumps(results,indent=2))

if __name__ == "__main__":
    main()
