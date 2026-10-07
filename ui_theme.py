"""Shared native blue/white design tokens for the approved A concept."""
INK = '#102553'
MUTED = '#526b94'
ACCENT = '#2064f5'
PALE = '#edf4ff'
LINE = '#dce8f8'
SUCCESS = '#138449'
WARNING = '#a56812'
FONT = 'Microsoft YaHei UI'
TITLE_SIZE = 22
METRIC_SIZE = 25
HP = '#df6170'
HP_PALE = '#fbeef0'

# Opt-in colors for the approved B panel; other native windows keep the tokens above.
MIDNIGHT = {
    'surface': '#152b40', 'raised': '#1c354b', 'ink': '#e3f3fd',
    'muted': '#a5bdcf', 'accent': '#91cfe9', 'line': '#355267',
    'track': '#2d495e', 'success': '#a1d7c4', 'warning': '#e8c88f',
    'bar': '#5a879f',
}


def format_damage(value):
    """Compact Chinese display units; accounting and exports keep raw integers."""
    if value >= 100000000:
        return f'{value/100000000:.2f}亿'
    if value >= 10000:
        amount=f'{value/10000:.2f}'
        return '1.00亿' if amount=='10000.00' else amount+'万'
    return f'{value:,.0f}'


def configure_history(style, scale=1):
    style.configure('History.TFrame', background='white')
    style.configure('History.TLabel', background='white', foreground=MUTED,
                    font=(FONT, -round(12*scale)))
    style.configure('HistoryTitle.TLabel', background='white', foreground=INK,
                    font=(FONT, -round(TITLE_SIZE*scale), 'bold'))
    style.configure('HistorySummary.TLabel', background='white', foreground=INK,
                    font=(FONT, -round(15*scale), 'bold'))
    style.configure('History.TButton', padding=(12, 8), font=(FONT, -round(12*scale)),
                    background=PALE, foreground=INK, borderwidth=0, relief='flat',
                    bordercolor=PALE,lightcolor=PALE,darkcolor=PALE,focuscolor=ACCENT)
    style.map('History.TButton',background=[('active','#dbe9ff'),('pressed','#c9defe')],
              foreground=[('disabled',MUTED)],bordercolor=[('focus',ACCENT)])
    style.configure('History.TCombobox',fieldbackground='white',background=PALE,
                    foreground=INK,arrowcolor=MUTED,bordercolor=LINE,lightcolor=LINE,darkcolor=LINE,
                    padding=(8,6),font=(FONT,-round(12*scale)))
    style.map('History.TCombobox',fieldbackground=[('readonly','white')],
              foreground=[('readonly',INK)],bordercolor=[('focus',ACCENT)])
    style.configure('History.Vertical.TScrollbar',background=LINE,troughcolor='white',
                    bordercolor='white',lightcolor=LINE,darkcolor=LINE,arrowcolor=MUTED,
                    borderwidth=0,relief='flat',arrowsize=round(10*scale))
    style.map('History.Vertical.TScrollbar',background=[('active','#bdd3f2')])
    style.configure('History.Treeview', background='white', fieldbackground='white',
                    foreground=INK, rowheight=round(30*scale), borderwidth=0,relief='flat',
                    bordercolor=LINE,lightcolor='white',darkcolor='white',
                    font=(FONT, -round(12*scale)))
    style.configure('History.Treeview.Heading', background=PALE, foreground=MUTED,
                    padding=(8, 8),borderwidth=0,relief='flat',bordercolor=PALE,lightcolor=PALE,darkcolor=PALE,
                    font=(FONT, -round(11*scale)))
    style.map('History.Treeview', background=[('selected', PALE)],
              foreground=[('selected', ACCENT)])
