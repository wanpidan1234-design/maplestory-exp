"""Native renderer for the approved B midnight observer panel.

Orb owns recording, history caches and animation lifetimes.  This view owns all
panel coordinates and native controls; auxiliary windows retain their own theme.
"""
import ctypes
import math
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk
from history_store import local_day
from monster_portraits import Portraits
from ui_charts import plot, rate_prefix, daily_chart_rows
from ui_theme import FONT, MIDNIGHT, format_damage


WIDTH = 570
HEIGHT = 910
FOOTER = 53
COMBAT_EXTRA = 228
NUMBER_FONT = 'Bahnschrift'


class MidnightPanel:
    def __init__(self, orb, window, scale, monitor):
        self.orb, self.window, self.scale, self.monitor = orb, window, scale, monitor
        self.palette = MIDNIGHT
        self.expanded = False
        self.closed = False
        self.note_extra = 0
        self.kill_scope = getattr(orb, 'kill_scope', '今日')
        self.kill_page = 0
        self.icons = {}
        self.controls = {}
        self.control_items = {}
        self.options = None
        self.drag_origin = None
        self.drag_monitor = None
        self.region_after = None
        self.region_token = None
        self.window_bindings = {}
        self.chart_boxes = dict(curve=(20, 398, 530, 74), bars=(20, 493, 530, 92),
                                daily=(20, 605, 530, 92), daycurve=(20,727,530,110))
        self.daily_rows = []
        self.layout_boxes = dict(header=(0,0,570,50),console=(20,68,530,185),
            session=(20,269,530,64),combat_summary=(20,333,530,35),
            combat_body=(20,368,530,228),footer=(0,HEIGHT-FOOTER,570,FOOTER))
        self.opacity_scale = None
        self.canvas = tk.Canvas(window, width=self.p(WIDTH), height=self.p(HEIGHT),
                                bg=MIDNIGHT['surface'], highlightthickness=1,
                                highlightbackground=MIDNIGHT['line'], bd=0)
        self.canvas.pack(fill='both', expand=True)
        self._scrollbar_style()
        self.scrollbar = ttk.Scrollbar(self.canvas, orient='vertical', command=self.canvas.yview,
                                       style='Midnight.Vertical.TScrollbar')
        self.canvas.configure(yscrollcommand=self.scrollbar.set, yscrollincrement=self.p(20))
        self._build()
        self.buttons = {name:self.controls[key] for name,key in dict(settings='settings',
            combat='combat_toggle',pause='pause',history='history',scales='multiscale',
            preview='preview',close='collapse').items()}
        self._bind_handles()
        self.canvas.bind('<ButtonPress-1>', self._drag_start)
        self.canvas.bind('<B1-Motion>', self._drag_move)
        self.canvas.bind('<ButtonRelease-1>', self._drag_end)
        for event,callback in (('<MouseWheel>',self._wheel),
                               ('<Configure>',self._schedule_region),
                               ('<Map>',self._mapped),
                               ('<Escape>',lambda event: orb.hide_panel())):
            self.window_bindings[event] = window.bind(event,callback,add='+')
        self._reflow()

    def p(self, value):
        return round(value * self.scale)

    def _scrollbar_style(self):
        # Import only the two neutral elements; the application's current theme
        # and every auxiliary window keep their existing style.
        style = ttk.Style(self.window)
        elements = style.element_names()
        for part in ('trough','thumb'):
            name = 'Midnight.Scrollbar.'+part
            if name not in elements:
                style.element_create(name,'from','clam','Vertical.Scrollbar.'+part)
        style.layout('Midnight.Vertical.TScrollbar',[
            ('Midnight.Scrollbar.trough',dict(sticky='ns',children=[
                ('Midnight.Scrollbar.thumb',dict(expand=True,sticky='nswe'))]))])
        style.configure('Midnight.Vertical.TScrollbar',background=MIDNIGHT['track'],
            troughcolor=MIDNIGHT['surface'],bordercolor=MIDNIGHT['surface'],
            lightcolor=MIDNIGHT['track'],darkcolor=MIDNIGHT['track'],borderwidth=0,
            relief='flat',width=self.p(10),arrowsize=self.p(10))
        style.map('Midnight.Vertical.TScrollbar',background=[('active',MIDNIGHT['muted']),
            ('pressed',MIDNIGHT['accent'])])

    def text(self, x, y, value, size=12, color=None, *, number=False, bold=False, **kw):
        return self.canvas.create_text(self.p(x), self.p(y), text=value,
            fill=color or MIDNIGHT['ink'], anchor=kw.pop('anchor', 'w'),
            font=(NUMBER_FONT if number else FONT, -self.p(size), 'bold' if bold else 'normal'), **kw)

    def line(self, points, color=None, width=1, **kw):
        return self.canvas.create_line(*[self.p(p) for p in points],
            fill=color or MIDNIGHT['line'], width=max(1, self.p(width)), **kw)

    def fit(self, item, value, size, width, color=None, *, number=True, bold=False):
        family = NUMBER_FONT if number else FONT
        font = tkfont.Font(family=family, size=-self.p(size), weight='bold' if bold else 'normal')
        while font.measure(value) > self.p(width) and size > 9:
            size -= .5
            font.configure(size=-self.p(size))
        options = dict(text=value, font=(family, -self.p(size), 'bold' if bold else 'normal'))
        if color:
            options['fill'] = color
        self.canvas.itemconfigure(item, **options)

    def icon(self, name, color=None):
        color = color or MIDNIGHT['muted']
        key = (name, color)
        if key in self.icons:
            return self.icons[key]
        # One authored line icon set, rendered at 3x for clean DPI scaling.
        unit = self.p(16) * 3 / 16
        image = Image.new('RGBA', (self.p(16)*3, self.p(16)*3))
        draw = ImageDraw.Draw(image)
        def line(points):
            draw.line([(round(x*unit), round(y*unit)) for x,y in points], fill=color,
                      width=max(1, round(1.1*unit)), joint='curve')
        def ellipse(box):
            draw.ellipse(tuple(round(v*unit) for v in box), outline=color, width=max(1, round(unit)))
        if name == 'crosshair':
            ellipse((4,4,12,12))
            for points in (((8,1),(8,4)),((8,12),(8,15)),((1,8),(4,8)),((12,8),(15,8))): line(points)
        elif name == 'settings':
            ellipse((5,5,11,11))
            line(((6,2),(10,2),(11,4),(14,5),(14,9),(12,10),(11,13),(7,14),(5,12),(2,11),(2,7),(4,6),(6,2)))
        elif name == 'close':
            line(((4,4),(12,12))); line(((12,4),(4,12)))
        elif name == 'shield':
            line(((8,2),(13,4),(13,9),(11,12),(8,14),(5,12),(3,9),(3,4),(8,2)))
        elif name == 'chevron': line(((4,6),(8,10),(12,6)))
        elif name == 'chevron_up': line(((4,10),(8,6),(12,10)))
        elif name == 'history':
            draw.arc(tuple(round(v*unit) for v in (3,2,14,14)), 190, 510, fill=color, width=max(1,round(unit)))
            line(((1,3),(2,7),(6,6))); line(((8,5),(8,9),(11,10)))
        elif name == 'chart':
            line(((2,2),(2,14),(14,14))); line(((4,11),(7,7),(10,9),(14,3)))
        elif name == 'bolt': line(((9,1),(3,9),(7,9),(6,15),(13,6),(9,6),(9,1)))
        elif name == 'pause':
            line(((5,3),(5,13))); line(((11,3),(11,13)))
        elif name == 'play': line(((5,3),(12,8),(5,13),(5,3)))
        photo = ImageTk.PhotoImage(image.resize((self.p(16),self.p(16)), Image.Resampling.LANCZOS), master=self.canvas)
        self.icons[key] = photo
        return photo

    def button(self, name, x, y, width, label, command, *, icon=None, height=30,
               primary=False, tags=(), parent=None):
        parent = parent or self.canvas
        bg = MIDNIGHT['accent'] if primary else parent.cget('background')
        fg = MIDNIGHT['surface'] if primary else MIDNIGHT['muted']
        widget = tk.Button(parent, text=label, command=command, bg=bg, fg=fg,
            activebackground='#b9e5f7' if primary else MIDNIGHT['raised'],
            activeforeground=MIDNIGHT['surface'] if primary else MIDNIGHT['ink'],
            font=(FONT,-self.p(12),'bold' if primary else 'normal'),
            relief='flat', bd=0, highlightthickness=1, highlightbackground=bg,
            highlightcolor=MIDNIGHT['accent'], cursor='hand2', takefocus=True,
            padx=self.p(5), pady=0, compound='left')
        if icon:
            widget.configure(image=self.icon(icon, fg))
        widget.bind('<Return>',lambda event: (widget.invoke(),'break')[1])
        self.controls[name] = widget
        if parent is self.canvas:
            item = self.canvas.create_window(self.p(x),self.p(y),window=widget,anchor='nw',
                width=self.p(width),height=self.p(height),tags=tags)
            self.control_items[name] = item
            widget.bind('<FocusIn>', lambda event, item=item: self._reveal(item))
        else:
            widget.place(x=self.p(x), y=self.p(y), width=self.p(width), height=self.p(height))
        return widget

    def _build(self):
        c = self.canvas
        c.create_rectangle(0,0,self.p(WIDTH),self.p(50),fill='#1b344a',outline='')
        self.line((0,50,570,50))
        c.create_image(self.p(27),self.p(26),image=self.icon('crosshair',MIDNIGHT['accent']))
        self.panel_title = self.text(44,26,'经验观察器',14,bold=True)
        self.status_dot = c.create_oval(*[self.p(v) for v in (339,23,344,28)],fill=MIDNIGHT['success'],outline='')
        self.session_state = self.text(351,26,'等待识别',11,MIDNIGHT['muted'])
        self.button('settings',488,11,29,'',self.toggle_options,icon='settings',height=29)
        self.button('collapse',526,11,29,'',self.orb.hide_panel,icon='close',height=29)
        for name in ('settings','collapse'):
            self.controls[name].configure(bg='#1b344a',highlightbackground='#1b344a')
        self.text(20,77,'当前分均',12,MIDNIGHT['muted'])
        self.panel_subtitle = self.text(258,77,'1min 窗口',12,MIDNIGHT['muted'],anchor='e')
        self.live_value = self.text(20,118,'—',48,number=True)
        self.live_unit = self.text(258,127,'经验 / 分',12,MIDNIGHT['muted'],anchor='e')
        self.live_hour = self.text(20,157,'折算 — / 时',12,MIDNIGHT['muted'])
        self.inline_rates, self.rate_labels, self.rate_progress = {}, {}, {}
        for seconds,x in ((120,20),(300,104),(1800,188)):
            self.rate_labels[seconds] = self.text(x,190,f'{seconds//60}min',12,MIDNIGHT['muted'])
            self.inline_rates[seconds] = self.text(x,212,'—',19,number=True)
            track = self.line((x,232,x+70,232),MIDNIGHT['track'],3,capstyle='round')
            fill = self.line((x,232,x,232),MIDNIGHT['accent'],3,capstyle='round',state='hidden')
            caption = self.text(x,242,'等待采样',9,MIDNIGHT['muted'],anchor='nw',width=self.p(77))
            self.rate_progress[seconds] = dict(track=track,fill=fill,caption=caption,x=x,y=232,width=70)
        self.line((280,68,280,253))
        self.text(302,77,'当前经验',12,MIDNIGHT['muted'])
        c.create_rectangle(self.p(509),self.p(66),self.p(550),self.p(88),outline=MIDNIGHT['line'])
        self.level = self.text(530,77,'Lv.—',12,number=True,anchor='center')
        self.current_value = self.text(302,114,'—',27,number=True)
        self.current_percent = self.text(550,118,'—',13,MIDNIGHT['muted'],number=True,anchor='e')
        self.exp_box = (302,140,248)
        self.line((302,140,550,140),MIDNIGHT['track'],4,capstyle='round')
        self.progress = self.line((302,140,302,140),MIDNIGHT['accent'],4,capstyle='round',state='hidden')
        self.hud_items = {}
        for name,y,color in (('hp',165,'#c293a5'),('mp',197,'#77aace')):
            self.text(302,y+2,name.upper(),12,number=True)
            item = self.text(332,y,'—',13,number=True)
            percent = self.text(550,y,'—',12,MIDNIGHT['muted'],number=True,anchor='e')
            self.line((332,y+13,550,y+13),MIDNIGHT['track'],4)
            bar = self.line((332,y+13,332,y+13),color,4,state='hidden')
            self.hud_items[name] = dict(value=item,bar=bar,percent=percent,x=332,y=y+13,width=218)
        c.create_rectangle(self.p(20),self.p(269),self.p(550),self.p(333),fill='#1d374e',outline='')
        self.line((20,269,550,269)); self.line((20,333,550,333))
        for name,x,label in (('total',32,'本次获得'),('today',164,'今日获得'),('minute',294,'全程分均'),('timer',410,'有效计时')):
            self.text(x,289,label,12,MIDNIGHT['muted'])
            setattr(self,name,self.text(x,311,'—',17 if name=='timer' else 18,number=True))
        self.button('combat_toggle',18,335,120,' 战斗明细',self.toggle_combat,icon='shield',height=31)
        self.kill_summary = self.text(514,351,'本次击杀 —',12,MIDNIGHT['muted'],anchor='e')
        self.chevron = c.create_image(self.p(540),self.p(351),image=self.icon('chevron'))
        c.tag_bind(self.chevron,'<Button-1>',lambda event:self.toggle_combat())
        self.line((20,368,550,368))
        self._build_combat()
        self.text(20,386,'观测分均',12,tags=('tail',))
        self.curve_caption = self.text(550,386,'最近 60 秒 · 北京时间',12,MIDNIGHT['muted'],anchor='e',tags=('tail',))
        self.text(20,483,'每分钟获得',12,tags=('tail',))
        self.bar_caption = self.text(550,483,'最近 12 分钟',12,MIDNIGHT['muted'],anchor='e',tags=('tail',))
        self.text(20,593,'每日获得经验',12,tags=('tail',))
        self.text(550,593,'近 7 天 · — 未记录',12,MIDNIGHT['muted'],anchor='e',tags=('tail',))
        self.text(20,713,'今日每分钟获得',12,tags=('tail',))
        self.day_caption=self.text(550,713,'北京时间 · 仅记录分钟',12,MIDNIGHT['muted'],anchor='e',tags=('tail',))
        self.detail = self.text(20,838,'',10,MIDNIGHT['muted'],anchor='nw',width=self.p(530),tags=('tail',))
        self.footer = tk.Frame(c,bg='#1a3248',highlightbackground=MIDNIGHT['line'],highlightthickness=1)
        self.footer.place(relx=0,rely=1,anchor='sw',relwidth=1,height=self.p(FOOTER))
        self.button('history',19,10,98,' 历史记录',self.orb.show_history,icon='history',height=32,parent=self.footer)
        self.button('multiscale',122,10,85,' 多尺度',self.orb.show_multiscale,icon='chart',height=32,parent=self.footer)
        self.button('preview',215,10,104,' 升级预览',self.orb.celebrate_level,icon='bolt',height=32,parent=self.footer)
        self.button('review',330,10,123,'后台评审',self.show_review,height=32,parent=self.footer)
        self.toggle_tag = self.button('pause',464,10,88,' 暂停',self.orb.toggle,icon='pause',height=32,primary=True,parent=self.footer)
        self.kill_portraits = Portraits(c)
        self.kill_photos = []

    def _build_combat(self):
        c = self.canvas
        before = set(c.find_all())
        self.combat_state = self.text(20,388,'本次 · 等待画面',11,MIDNIGHT['muted'])
        self.button('combat_full',436,374,114,'完整战斗窗口',self.orb.show_combat,height=28)
        for name,x,label in (('damage_dealt',20,'已识别造成伤害'),('damage_taken',160,'已识别受到伤害'),('kills_current',300,'本次击杀'),('kills_today',432,'今日击杀')):
            self.text(x,418,label,11,MIDNIGHT['muted'])
            setattr(self,name,self.text(x,443,'—',20,number=True))
        self.combat_vitals = {}
        for name,x,label in (('hp',20,'HP'),('mp',300,'MP')):
            self.combat_vitals[name] = self.text(x,476,f'{label} 消耗 — · 恢复 —',11,MIDNIGHT['muted'])
        self.line((20,496,550,496))
        self.text(20,514,'击杀分类',12)
        self.button('kill_scope',359,500,70,self.kill_scope,self.toggle_kill_scope,height=27)
        self.button('kill_next',445,500,104,'下一页',self.next_kill_page,height=27)
        self.kill_tiles = []
        for x in (20,290):
            pic = c.create_image(self.p(x),self.p(550),anchor='w')
            name = self.text(x+48,541,'等待怪物画面',11)
            number = self.text(x+48,561,'',13,MIDNIGHT['accent'],number=True)
            self.kill_tiles.append((pic,name,number))
        self.kill_hint = self.text(20,583,'',10,MIDNIGHT['muted'])
        self.line((20,596,550,596))
        for item in set(c.find_all())-before:
            c.addtag_withtag('combat',item)
        c.itemconfigure('combat',state='hidden')

    def _bind_handles(self):
        for name in ('panel_title','panel_subtitle','session_state','status_dot','timer',
                     'live_value','live_hour','current_value','current_percent','progress',
                     'total','today','minute','detail','toggle_tag','inline_rates',
                     'rate_labels','rate_progress','curve_caption','bar_caption'):
            setattr(self.orb,name,getattr(self,name))
        self.orb.panel_canvas = self.canvas

    def _reflow(self):
        total = HEIGHT + (COMBAT_EXTRA if self.expanded else 0) + self.note_extra
        self.content_height = total
        max_height = max(self.p(FOOTER+120), self.monitor['height']-36)
        height = min(self.p(total),max_height)
        self.window.geometry(f'{self.p(WIDTH)}x{height}')
        self._schedule_region()
        self.canvas.configure(scrollregion=(0,0,self.p(WIDTH),self.p(total)))
        self._sync_viewport(height)
        if self.window.winfo_viewable():
            x = max(self.monitor['left'],min(self.window.winfo_x(),self.monitor['left']+self.monitor['width']-self.p(WIDTH)))
            y = max(self.monitor['top'],min(self.window.winfo_y(),self.monitor['top']+self.monitor['height']-height-10))
            self.orb.position(self.window,x,y)

    def _sync_viewport(self, height):
        """Keep added charts reachable when restoring a smaller saved window."""
        self.visible_height = height/self.scale
        self.layout_boxes['footer'] = (0,self.visible_height-FOOTER,WIDTH,FOOTER)
        if height < self.p(self.content_height):
            self.scrollbar.place(x=self.p(556),y=self.p(51),width=self.p(12),height=height-self.p(FOOTER+53))
            self.scrollbar.lift()
        else:
            self.scrollbar.place_forget()
            self.canvas.yview_moveto(0)

    def _mapped(self,event):
        if event.widget is self.window:
            self.region_token = None
            self._schedule_region()

    def _schedule_region(self,event=None):
        if self.closed or (event is not None and event.widget is not self.window):
            return
        if self.region_after is None:
            self.region_after = self.window.after_idle(self._round_window)

    def _round_window(self):
        """Clip the borderless Windows surface to B's 13px corners."""
        self.region_after = None
        if self.closed:
            return
        try:
            user, gdi = ctypes.windll.user32, ctypes.windll.gdi32
            handle = ctypes.c_void_p
            user.GetAncestor.argtypes = [handle,ctypes.c_uint]
            user.GetAncestor.restype = handle
            user.SetWindowRgn.argtypes = [handle,handle,ctypes.c_int]
            user.SetWindowRgn.restype = ctypes.c_int
            gdi.CreateRoundRectRgn.argtypes = [ctypes.c_int]*6
            gdi.CreateRoundRectRgn.restype = handle
            gdi.DeleteObject.argtypes = [handle]
            # Tk may replace its native wrapper while realizing a geometry
            # request. Resolve the current wrapper and realized client size.
            hwnd = user.GetAncestor(self.window.winfo_id(),2)
            width,height = self.window.winfo_width(),self.window.winfo_height()
            token = (hwnd,width,height,self.scale)
            if token == self.region_token or width <= 1 or height <= 1:
                return
            self._sync_viewport(height)
            region = gdi.CreateRoundRectRgn(0,0,width+1,height+1,self.p(26),self.p(26))
            if region:
                # Successful SetWindowRgn transfers region ownership to Windows.
                if user.SetWindowRgn(hwnd,region,1):
                    self.region_token = token
                else:
                    gdi.DeleteObject(region)
        except (AttributeError,OSError,tk.TclError):
            pass

    def toggle_combat(self):
        self.set_expanded(not self.expanded)

    def set_expanded(self, expanded, *, refresh=True):
        if expanded == self.expanded:
            return
        if refresh and self.orb.panel_level and self.orb.panel_level.active:
            self.orb.panel_level.finish()
        self.expanded = expanded
        self.canvas.itemconfigure('combat',state='normal' if self.expanded else 'hidden')
        self.canvas.itemconfigure(self.chevron,image=self.icon('chevron_up' if expanded else 'chevron'))
        self.canvas.move('tail',0,self.p(COMBAT_EXTRA if self.expanded else -COMBAT_EXTRA))
        self._reflow()
        if refresh:
            self.paint_combat()
            self.orb.paint_minutes()
            self.orb.refresh_panel_gains()

    def toggle_kill_scope(self):
        self.kill_scope = '本次' if self.kill_scope=='今日' else '今日'
        self.orb.kill_scope = self.kill_scope
        self.kill_page = 0
        self.controls['kill_scope'].configure(text=self.kill_scope)
        self.paint_combat()

    def next_kill_page(self):
        self.kill_page += 1
        self.paint_combat()

    def _wheel(self,event):
        if self.content_height > self.visible_height:
            self.canvas.yview_scroll(-int(event.delta/120),'units')
            return 'break'

    def _reveal(self,item):
        if self.content_height <= self.visible_height:
            return
        y = self.canvas.coords(item)[1]/self.scale
        top = self.canvas.canvasy(0)/self.scale
        if y < top or y+32 > top+self.visible_height-FOOTER:
            target = max(0,y-self.visible_height+FOOTER+42) if y>=top else max(0,y-10)
            self.canvas.yview_moveto(target/self.content_height)

    def _drag_start(self,event):
        if self.canvas.canvasy(event.y) <= self.p(49):
            self.drag_origin = (event.x_root,event.y_root,self.window.winfo_x(),self.window.winfo_y())
            self.drag_monitor = self.monitor
        elif self.options:
            self.toggle_options()

    def _drag_move(self,event):
        if not self.drag_origin:
            return
        from game_capture import choose_monitor, get_monitors
        px,py,x,y = self.drag_origin
        x,y = x+event.x_root-px,y+event.y_root-py
        monitor = choose_monitor(dict(left=event.x_root,top=event.y_root,width=1,height=1),get_monitors())
        self.drag_monitor = monitor
        x = max(monitor['left'],min(x,monitor['left']+monitor['width']-self.window.winfo_width()))
        y = max(monitor['top'],min(y,monitor['top']+monitor['height']-self.window.winfo_height()))
        self.orb.position(self.window,x,y)

    def _drag_end(self,event):
        if not self.drag_origin:
            return
        self.drag_origin = None
        monitor = self.drag_monitor or self.monitor
        self.drag_monitor = None
        if monitor != self.monitor:
            self.orb.refit_panel(monitor,self.window.winfo_x(),self.window.winfo_y())

    def toggle_options(self):
        if self.options:
            self.options.destroy()
            self.options = None
            self.opacity_scale = None
            return
        frame = self.options = tk.Frame(self.canvas,bg=MIDNIGHT['raised'],
            highlightthickness=1,highlightbackground=MIDNIGHT['line'])
        frame.place(x=self.p(294),y=self.p(49),width=self.p(261),height=self.p(128))
        tk.Label(frame,text='悬浮球透明度',bg=MIDNIGHT['raised'],fg=MIDNIGHT['ink'],
                 font=(FONT,-self.p(12))).place(x=self.p(12),y=self.p(9))
        slider = tk.Scale(frame,from_=45,to=100,resolution=1,orient='horizontal',
            bg=MIDNIGHT['raised'],fg=MIDNIGHT['ink'],troughcolor=MIDNIGHT['track'],
            activebackground=MIDNIGHT['accent'],highlightthickness=1,
            highlightbackground=MIDNIGHT['raised'],highlightcolor=MIDNIGHT['accent'],
            bd=0,sliderrelief='flat',font=(NUMBER_FONT,-self.p(11)),takefocus=True)
        slider.set(round(self.orb.alpha*100))
        slider.configure(command=self.orb.set_opacity)
        self.opacity_scale = slider
        slider.place(x=self.p(12),y=self.p(31),width=self.p(235),height=self.p(49))
        self.button('recognition',12,88,235,'打开识别设置',self.orb.show_settings,height=28,parent=frame)
        slider.focus_set()

    def toggle_settings(self):
        self.toggle_options()

    def show_review(self):
        view = getattr(self.orb, 'review_view', None)
        if view is not None:
            view.show_details()

    def paint(self, snapshot, window_display):
        b,c = self.orb.backend,self.canvas
        state, _ = self.orb.state(snapshot)
        rate = snapshot['primary']
        retrying = snapshot.get('display_state')=='retrying'
        restored = snapshot.get('display_state')=='restored'
        visibility_paused = snapshot.get('display_state')=='visibility_paused'
        held = retrying or restored or visibility_paused
        color = MIDNIGHT['success'] if state=='识别正常' else MIDNIGHT['muted'] if state=='已暂停' else MIDNIGHT['warning']
        if state=='采样中':
            state = f"采样 {math.floor(rate['observed_seconds']*10)/10:.1f} / 60 秒"
        self.fit(self.session_state,state,11,130,color,number=False)
        c.itemconfigure(self.status_dot,fill=color)
        elapsed = int(b.tracker.elapsed)
        self.fit(self.timer,f'{elapsed//3600:02}:{elapsed//60%60:02}:{elapsed%60:02}',17,124)
        minute,_ = b.tracker.rates()
        known = b.tracker.previous is not None
        totals_known = known or bool(getattr(b,'records',None)) or b.tracker.total>0 or b.tracker.elapsed>0
        fresh = known and b.running and not held and b.fresh_reading()
        self.fit(self.total,f'{b.tracker.total:,.0f}' if totals_known else '—',18,119)
        self.fit(self.minute,f'{minute:,.0f}' if totals_known else '—',18,104)
        days = b.history.days()
        today_key = local_day(time.time())
        self.daily_rows = daily_chart_rows(days,today_key)
        today = next((r['gain'] for r in days if r['day']==today_key),0)
        self.fit(self.today,f'{today:,}',18,117)
        realtime,prefix = rate['value'],rate_prefix(rate,held)
        unit_box = c.bbox(self.live_unit)
        unit_width = unit_box[2]-unit_box[0]
        number_width = (self.p(238)-unit_width-self.p(10))/self.scale
        self.fit(self.live_value,f'{prefix}{realtime:,.0f}' if realtime is not None else '—',48,number_width,
                 MIDNIGHT['warning'] if held else MIDNIGHT['ink'])
        bounds = c.bbox(self.live_value)
        c.coords(self.live_unit,bounds[2]+self.p(7)+unit_width,self.p(127))
        projected = f'{prefix}{realtime*60:,.0f}' if realtime is not None else '—'
        self.fit(self.live_hour,f'折算 {projected} / 时',12,238,
                 MIDNIGHT['warning'] if held else MIDNIGHT['muted'],number=False)
        for seconds,item in self.inline_rates.items():
            sample = window_display(snapshot,seconds)
            controls = self.rate_progress[seconds]
            value_color = MIDNIGHT['warning'] if sample['retrying'] or sample['restored'] or sample.get('interrupted') else MIDNIGHT['ink'] if sample['available'] else MIDNIGHT['muted']
            self.fit(item,sample['number'],19,72,value_color)
            x,y,width = controls['x'],controls['y'],controls['width']
            c.coords(controls['fill'],self.p(x),self.p(y),self.p(x+width*sample['ratio']),self.p(y))
            c.itemconfigure(controls['fill'],state='normal' if sample['ratio']>0 else 'hidden',fill=value_color)
            c.itemconfigure(controls['caption'],text=sample['caption'],fill=value_color if held else MIDNIGHT['muted'])
        self.fit(self.current_value,f'{b.tracker.previous:,}' if fresh else '—',27,175)
        pct = b.latest.percent if fresh and b.latest else None
        self.fit(self.current_percent,f'{pct:.2f}%' if pct is not None else '待识别',13,70,MIDNIGHT['muted'],number=pct is not None)
        level = getattr(b.tracker,'level',None)
        self.fit(self.level,f'Lv.{level}' if level is not None else 'Lv.—',12,36)
        x,y,width = self.exp_box
        c.coords(self.progress,self.p(x),self.p(y),self.p(x+width*max(0,min(100,pct or 0))/100),self.p(y))
        c.itemconfigure(self.progress,state='normal' if pct is not None else 'hidden')
        self.toggle_tag.configure(text=' 处理变化' if b.tracker.blocked else ' 暂停' if b.running else ' 继续',
            image=self.icon('pause' if b.running else 'play',MIDNIGHT['surface']))
        if b.tracker.blocked:
            note='经验下降已暂停 · 点击“处理变化”确认升级或跳过'
        elif visibility_paused:
            note=('切屏暂停 · 已保留采样进度' if snapshot.get('visibility_preserved')
                  else '切屏暂停 · 等待新的可信读数')
        elif restored:
            note='已恢复上次分均与采样进度 · 等待新读数，离线时间不计入'
        elif not b.running:
            note='已暂停 · 历史记录已保存'
        elif retrying:
            note=f"本帧识别未通过，正在重试 · 暂显 {snapshot['display_age_seconds']:.1f} 秒前有效分均"
        elif not fresh:
            note='未找到游戏窗口 · 打开游戏后将自动识别' if '未找到' in b.status.get() else '等待有效读数 · 请保持游戏经验栏可见'
            if getattr(getattr(b,'auto_level',None),'wait_since',None) is not None:
                note='正在核验等级变化 · 连续多帧确认后继续'
        elif getattr(b.tracker,'level_gaps',0):
            note='≥ 仅含已确认经验 · 升级尾段未知，原采样进度保留'
        else:
            note=''
        c.itemconfigure(self.detail,text=note,fill=MIDNIGHT['muted'] if fresh else color)
        # The design reserves one note line; longer status messages grow naturally.
        box=c.bbox(self.detail)
        extra=max(0,math.ceil((box[3]-box[1])/self.scale)-15) if note and box else 0
        if extra!=self.note_extra:
            self.note_extra=extra
            self._reflow()
        self.paint_combat()

    def paint_combat(self):
        c = self.canvas
        service = getattr(self.orb.backend,'combat',None)
        known = service is not None and service.tracker.frame_count>0
        fresh = bool(service and service.running and service.last_reading and time.time()-service.last_reading<2)
        totals = service.tracker.totals if service else {}
        for name,controls in self.hud_items.items():
            reading = service.tracker.vitals[name] if fresh else None
            self.fit(controls['value'],f'{reading[0]:,} / {reading[1]:,}' if reading else '—',13,157)
            ratio = max(0,min(1,reading[0]/reading[1])) if reading and reading[1] else 0
            c.itemconfigure(controls['percent'],text=f'{ratio:.0%}' if reading else '—')
            x,y,width = controls['x'],controls['y'],controls['width']
            c.coords(controls['bar'],self.p(x),self.p(y),self.p(x+width*ratio),self.p(y))
            c.itemconfigure(controls['bar'],state='normal' if reading and ratio>0 else 'hidden')
        kills = service.kill_summary() if service and hasattr(service,'kill_summary') else None
        current = (kills or {}).get('totals',{})
        today = (kills or {}).get('today',current)
        c.itemconfigure(self.kill_summary,text=f'本次击杀 {sum(current.values()):,}')
        if not self.expanded:
            return
        c.itemconfigure(self.combat_state,text='本次 · 画面估算' if fresh else '本次 · 已暂停' if service and not service.running else '本次 · 等待画面')
        for name in ('damage_dealt','damage_taken'):
            self.fit(getattr(self,name),format_damage(totals.get(name,0)) if known else '—',20,128)
        self.fit(self.kills_current,f'{sum(current.values()):,}',20,116)
        self.fit(self.kills_today,f'{sum(today.values()):,}',20,116)
        for name,item in self.combat_vitals.items():
            spent = format_damage(totals.get(name+'_spent',0)) if known else '—'
            restored = format_damage(totals.get(name+'_restored',0)) if known else '—'
            self.fit(item,f'{name.upper()} 消耗 {spent} · 恢复 {restored}',11,249,MIDNIGHT['muted'],number=False)
        counts = today if self.kill_scope=='今日' else current
        entries = sorted(((name,value) for name,value in counts.items() if value>0),key=lambda row:(row[0]=='未识别怪物',-row[1],row[0]))
        pages = max(1,(len(entries)+1)//2)
        self.kill_page %= pages
        self.kill_photos=[]
        for index,(pic,name,number) in enumerate(self.kill_tiles):
            offset=self.kill_page*2+index
            if offset<len(entries):
                label,count=entries[offset]
                photo=self.kill_portraits.get(label,self.p(40))
                self.kill_photos.append(photo)
                c.itemconfigure(pic,image=photo,state='normal')
                self.fit(name,label,11,192,number=False)
                c.itemconfigure(number,text=f'{count:,} 只')
            else:
                c.itemconfigure(pic,state='hidden')
                c.itemconfigure(name,text='等待怪物画面' if not entries and index==0 else '')
                c.itemconfigure(number,text='')
        c.itemconfigure(self.kill_hint,text=f'{self.kill_scope} · {self.kill_page+1}/{pages} 页 · 灰色为未匹配素材')
        self.controls['kill_next'].configure(state='normal' if pages>1 else 'disabled')

    def paint_minutes(self, curve, bars, time_range, day_curve=()):
        offset = COMBAT_EXTRA if self.expanded else 0
        for name,rows,is_bars in (('curve',curve,False),('bars',bars,True),('daily',self.daily_rows,True)):
            x,y,width,height = self.chart_boxes[name]
            plot(self.canvas,(x,y+offset,width,height),rows,bars=is_bars,scale=self.scale,
                 tag=name,time_range=None if is_bars else time_range,palette=MIDNIGHT,font_size=11,
                 bar_labels=is_bars,bar_slots=7 if name=='daily' else 12)
        x,y,width,height=self.chart_boxes['daycurve']
        plot(self.canvas,(x,y+offset,width,height),day_curve,scale=self.scale,tag='daycurve',
             palette=MIDNIGHT,font_size=11,indexed=True,max_gap=None)
        self.canvas.itemconfigure(self.day_caption,text=f'北京时间 · 已记录 {len(day_curve)} 分钟')

    def gain_lanes(self):
        lanes = {'exp':(548,102,100,MIDNIGHT['accent'],8,'e')}
        if self.expanded:
            lanes.update(damage_dealt=(147,430,80,MIDNIGHT['accent'],8,'e'),
                         damage_taken=(287,430,80,'#c293a5',8,'e'))
        return lanes

    def close(self):
        self.closed = True
        if self.region_after is not None:
            try:
                self.window.after_cancel(self.region_after)
            except tk.TclError:
                pass
            self.region_after = None
        for event,binding in self.window_bindings.items():
            try:
                self.window.unbind(event,binding)
            except tk.TclError:
                pass
        self.window_bindings.clear()
        if self.options:
            self.options.destroy()
            self.options = None
            self.opacity_scale = None
