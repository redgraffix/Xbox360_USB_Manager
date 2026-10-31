"""Flat Xbox palette with stable dimensions and explicit interaction colors."""
import ttkbootstrap as bootstrap

BG='#151b18';PANEL='#202a24';FG='#edf1ed';MUTED='#a9b8ad';GREEN='#92d34f';LINE='#35453a'


def apply(root):
    style=bootstrap.Style(theme='everforest-dark')
    style.configure('.',font=('Segoe UI',10),background=BG,foreground=FG)
    for widget in ('TFrame','TLabel','TCheckbutton','TNotebook'):style.configure(widget,background=BG,foreground=FG)
    style.configure('Muted.TLabel',foreground=MUTED)
    style.configure('TButton',padding=(14,8),borderwidth=0,background=LINE,foreground=FG)
    style.map('TButton',background=[('disabled','#26352a'),('pressed','#3e5545'),('active','#405548')],foreground=[('disabled','#91a297'),('active',FG)])
    style.configure('Accent.TButton',padding=(14,8),borderwidth=0,background=GREEN,foreground='#112009')
    style.map('Accent.TButton',background=[('disabled','#425938'),('pressed','#7cb83e'),('active','#a0dc66')],foreground=[('disabled','#a9b8ad'),('active','#112009')])
    style.configure('TNotebook',tabmargins=0,borderwidth=0)
    style.configure('TNotebook.Tab',padding=(22,10),background=PANEL,foreground=FG,borderwidth=0)
    style.map('TNotebook.Tab',padding=[('selected',(22,10)),('!selected',(22,10))],
              expand=[('selected',(0,0,0,0)),('!selected',(0,0,0,0))],
              background=[('selected',LINE),('active','#2d3b32')],foreground=[('selected',GREEN),('active',FG)])
    style.configure('Treeview',background=PANEL,fieldbackground=PANEL,foreground=FG,rowheight=34,borderwidth=0)
    style.configure('Games.Treeview',rowheight=48)
    style.configure('Treeview.Heading',background=LINE,foreground=FG,font=('Segoe UI Semibold',10),padding=8,relief='flat')
    style.map('Treeview.Heading',background=[('pressed','#425b49'),('active','#3e5144')],foreground=[('active',FG),('pressed',FG)])
    style.map('Treeview',background=[('selected','#38583e')],foreground=[('selected',FG)])
    style.configure('TCombobox',padding=(10,7),arrowsize=18,fieldbackground=PANEL,foreground=FG)
    style.map('TCombobox',fieldbackground=[('readonly',PANEL)],foreground=[('readonly',FG)],
              selectbackground=[('readonly',PANEL)],selectforeground=[('readonly',FG)])
    style.map('TCheckbutton',background=[('active',BG)],foreground=[('disabled',MUTED),('active',FG)])
    style.configure('TEntry',fieldbackground=PANEL,foreground=FG,padding=7)
    style.configure('Horizontal.TProgressbar',background=GREEN,troughcolor=LINE,borderwidth=0)
    root.option_add('*TCombobox*Listbox.background',PANEL)
    root.option_add('*TCombobox*Listbox.foreground',FG)
    root.option_add('*TCombobox*Listbox.selectBackground','#38583e')
    root.option_add('*TCombobox*Listbox.selectForeground',FG)
    return style
