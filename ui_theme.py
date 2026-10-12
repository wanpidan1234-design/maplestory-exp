"""The approved B Midnight palette shared by every native observer window."""
MIDNIGHT = {
    'surface': '#152b40', 'raised': '#1c354b', 'ink': '#e3f3fd',
    'muted': '#a5bdcf', 'accent': '#91cfe9', 'line': '#355267',
    'track': '#2d495e', 'success': '#a1d7c4', 'warning': '#e8c88f',
    'bar': '#5a879f', 'field': '#102334', 'hover': '#284a61',
    'pressed': '#365b73', 'selection': '#2d536b',
    'control_line': '#63869d', 'disabled': '#7892a5',
    'hp': '#ef9fa7', 'hp_track': '#493443',
}
SURFACE = MIDNIGHT['surface']
INK = MIDNIGHT['ink']
MUTED = MIDNIGHT['muted']
ACCENT = MIDNIGHT['accent']
PALE = MIDNIGHT['raised']
LINE = MIDNIGHT['line']
SUCCESS = MIDNIGHT['success']
WARNING = MIDNIGHT['warning']
FONT = 'Microsoft YaHei UI'
TITLE_SIZE = 22
METRIC_SIZE = 25
HP = MIDNIGHT['hp']
HP_PALE = MIDNIGHT['hp_track']


def format_damage(value):
    """Compact Chinese display units; accounting and exports keep raw integers."""
    if value >= 100000000:
        return f'{value/100000000:.2f}亿'
    if value >= 10000:
        amount=f'{value/10000:.2f}'
        return '1.00亿' if amount=='10000.00' else amount+'万'
    return f'{value:,.0f}'


def configure_history(style, scale=1):
    """Install both native defaults and the existing scoped style names."""
    p=lambda value:round(value*scale)
    style.configure('.',background=SURFACE,foreground=INK,font=(FONT,-p(12)),
                    selectbackground=MIDNIGHT['selection'],selectforeground=INK,
                    insertcolor=ACCENT,troughcolor=SURFACE,bordercolor=LINE,
                    lightcolor=SURFACE,darkcolor=SURFACE,focuscolor=ACCENT)
    for prefix in ('','History.'):
        style.configure(prefix+'TFrame',background=SURFACE)
        style.configure(prefix+'TLabel',background=SURFACE,foreground=MUTED,font=(FONT,-p(12)))
        style.configure(prefix+'TButton',padding=(p(12),p(7)),font=(FONT,-p(12)),
            background=PALE,foreground=INK,borderwidth=1,relief='flat',
            bordercolor=MIDNIGHT['control_line'],lightcolor=PALE,darkcolor=PALE,focuscolor=ACCENT)
        style.map(prefix+'TButton',background=[('disabled',PALE),('pressed',MIDNIGHT['pressed']),('active',MIDNIGHT['hover'])],
            foreground=[('disabled',MIDNIGHT['disabled'])],bordercolor=[('focus',ACCENT)])
        for control in ('TCombobox','TEntry'):
            style.configure(prefix+control,fieldbackground=MIDNIGHT['field'],background=PALE,
                foreground=INK,arrowcolor=MUTED,bordercolor=MIDNIGHT['control_line'],
                lightcolor=MIDNIGHT['field'],darkcolor=MIDNIGHT['field'],insertcolor=ACCENT,
                selectbackground=MIDNIGHT['selection'],selectforeground=INK,padding=(p(8),p(6)),font=(FONT,-p(12)))
            style.map(prefix+control,fieldbackground=[('disabled',PALE),('readonly',MIDNIGHT['field'])],
                foreground=[('disabled',MIDNIGHT['disabled']),('readonly',INK)],
                bordercolor=[('focus',ACCENT),('active',ACCENT)])
        for orientation in ('Vertical','Horizontal'):
            name=prefix+orientation+'.TScrollbar'
            style.configure(name,background=MIDNIGHT['control_line'],troughcolor=SURFACE,
                bordercolor=SURFACE,lightcolor=MIDNIGHT['control_line'],darkcolor=MIDNIGHT['control_line'],
                arrowcolor=INK,borderwidth=0,relief='flat',arrowsize=p(11))
            style.map(name,background=[('pressed',ACCENT),('active',MUTED)])
        style.configure(prefix+'Treeview',background=SURFACE,fieldbackground=SURFACE,
            foreground=INK,rowheight=p(30),borderwidth=0,relief='flat',bordercolor=LINE,
            lightcolor=SURFACE,darkcolor=SURFACE,font=(FONT,-p(12)))
        style.configure(prefix+'Treeview.Heading',background=PALE,foreground=MUTED,
            padding=(p(8),p(8)),borderwidth=0,relief='flat',bordercolor=PALE,
            lightcolor=PALE,darkcolor=PALE,font=(FONT,-p(11)))
        style.map(prefix+'Treeview',background=[('selected',MIDNIGHT['selection'])],foreground=[('selected',INK)])
        style.map(prefix+'Treeview.Heading',background=[('active',MIDNIGHT['hover'])])
    for name,size in (('HistoryTitle.TLabel',TITLE_SIZE),('Title.TLabel',TITLE_SIZE),('HistorySummary.TLabel',15)):
        style.configure(name,background=SURFACE,foreground=INK,font=(FONT,-p(size),'bold'))
    style.configure('Sub.TLabel',background=SURFACE,foreground=MUTED,font=(FONT,-p(11)))
    style.configure('TCheckbutton',background=SURFACE,foreground=INK,indicatorbackground=MIDNIGHT['field'],
                    indicatorforeground=ACCENT,font=(FONT,-p(12)),padding=p(4))
    style.map('TCheckbutton',background=[('active',SURFACE)],foreground=[('disabled',MIDNIGHT['disabled'])],
              indicatorbackground=[('selected',ACCENT)],indicatorforeground=[('selected',SURFACE)])
    style.configure('TPanedwindow',background=SURFACE)
    style.configure('Sash',sashthickness=p(6),background=LINE)
    style.configure('TSeparator',background=LINE)
    style.configure('Primary.TButton',background=ACCENT,foreground=SURFACE,bordercolor=ACCENT)
    style.map('Primary.TButton',background=[('disabled',PALE),('pressed',MUTED),('active',INK)],
              foreground=[('disabled',MIDNIGHT['disabled']),('!disabled',SURFACE)])


def configure_window(window):
    """Theme native chrome and dropdown popups while retaining OS affordances."""
    window.configure(background=SURFACE)
    for option,value in (
        ('background',MIDNIGHT['field']),('foreground',INK),
        ('selectBackground',MIDNIGHT['selection']),('selectForeground',INK)):
        window.option_add('*TCombobox*Listbox.'+option,value)

    def chrome(event=None):
        if event is not None and event.widget is not window:return
        try:
            import ctypes
            import tkinter as tk
            if not hasattr(ctypes,'windll'):return
            user=ctypes.windll.user32
            user.GetParent.argtypes=[ctypes.c_void_p]
            user.GetParent.restype=ctypes.c_void_p
            hwnd=user.GetParent(window.winfo_id()) or window.winfo_id()
            setter=ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_void_p,ctypes.c_uint]
            enabled=ctypes.c_int(1)
            if setter(hwnd,20,ctypes.byref(enabled),ctypes.sizeof(enabled)):
                setter(hwnd,19,ctypes.byref(enabled),ctypes.sizeof(enabled))
            for attribute,color in ((35,PALE),(36,INK),(34,LINE)):
                rgb=tuple(int(color[index:index+2],16) for index in (1,3,5))
                value=ctypes.c_uint(rgb[0]|rgb[1]<<8|rgb[2]<<16)
                setter(hwnd,attribute,ctypes.byref(value),ctypes.sizeof(value))
        except (OSError,AttributeError,RuntimeError,tk.TclError):
            pass  # Older Windows versions keep their normal titlebar.
    window.bind('<Map>',chrome,add='+')
    window.after_idle(chrome)
