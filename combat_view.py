"""Blue/white combat ledger, with explicit visual-estimate semantics."""
import csv
import time
from app_time import format_time
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from ui_theme import FONT,INK,MUTED,ACCENT,PALE,LINE,HP,HP_PALE,TITLE_SIZE,METRIC_SIZE,configure_history,format_damage
from combat_service import LABELS
from combat_tracker import KINDS
from ui_feedback import CanvasGains,DeltaWatch
from monster_portraits import Portraits
from PIL import ImageTk
from ui_damage import damage_image


class CombatWindow:
    def __init__(self,root,service):
        self.service=service
        self.window=tk.Toplevel(root)
        self.window.title('经验观察器 · 战斗与消耗')
        self.window.configure(bg='white')
        self.window.attributes('-topmost',True)
        self.window.geometry('1100x1080')
        self.window.minsize(850,850)
        self.scale=max(1,root.winfo_fpixels('1i')/96)
        style=ttk.Style();configure_history(style,self.scale)
        style.configure('Combat.TNotebook',background='white',borderwidth=0)
        style.configure('Combat.TNotebook.Tab',background=PALE,foreground=MUTED,padding=(16,8),font=(FONT,-round(12*self.scale)))
        style.map('Combat.TNotebook.Tab',foreground=[('selected',ACCENT)],background=[('selected','white')])
        style.configure('Combat.HP.Horizontal.TProgressbar',background=HP,troughcolor=HP_PALE,
                        borderwidth=0,lightcolor=HP,darkcolor=HP,bordercolor=HP_PALE)
        style.configure('Combat.MP.Horizontal.TProgressbar',background=ACCENT,troughcolor=PALE,
                        borderwidth=0,lightcolor=ACCENT,darkcolor=ACCENT,bordercolor=PALE)
        outer=tk.Frame(self.window,bg='white',padx=28,pady=18);outer.pack(fill='both',expand=True)
        self.label(outer,'战斗与消耗',TITLE_SIZE,True,INK).pack(anchor='w')
        self.label(outer,'单人刷怪 · 仅统计画面中识别到的飘字和血蓝变化',11).pack(anchor='w',pady=(6,10))
        controls=tk.Frame(outer,bg='white');controls.pack(fill='x')
        self.label(controls,'查看范围',11).pack(side='left')
        self.selection=tk.StringVar(value='本次')
        self.days=ttk.Combobox(controls,textvariable=self.selection,values=['本次'],state='readonly',width=18,style='History.TCombobox')
        self.days.pack(side='left',padx=12);self.days.bind('<<ComboboxSelected>>',lambda e:self.refresh())
        ttk.Button(controls,text='导出所选记录',style='History.TButton',command=self.export).pack(side='right')
        self.label(outer,'自动保存 · 本次跟随经验统计的暂停 / 继续 · 每日结果可回看',10).pack(side='bottom',anchor='w',pady=(12,0))
        self.tabs=ttk.Notebook(outer,style='Combat.TNotebook');self.tabs.pack(fill='both',expand=True,pady=(12,0))
        content=tk.Frame(self.tabs,bg='white');self.tabs.add(content,text='伤害与血蓝')
        gauges=tk.Frame(content,bg='white');gauges.pack(fill='x',pady=(12,8))
        self.vital_labels={};self.bars={}
        for name,title in [('hp','当前血量 HP'),('mp','当前蓝量 MP')]:
            f=tk.Frame(gauges,bg='white');f.pack(side='left',fill='x',expand=True,padx=(0,20))
            self.vital_labels[name]=self.label(f,title+'   —',13,True,INK);self.vital_labels[name].pack(anchor='w',pady=(0,8))
            self.bars[name]=ttk.Progressbar(f,maximum=100,style=f'Combat.{name.upper()}.Horizontal.TProgressbar')
            self.bars[name].pack(fill='x')
        metrics=tk.Frame(content,bg='white');metrics.pack(fill='x',pady=(0,6))
        self.metrics={}
        self.damage_photos={};self.damage_image_keys={}
        self.gain_watch=DeltaWatch();self.gain_canvases={};self.gain_animations={}
        titles={'damage_dealt':'已识别造成伤害','damage_taken':'已识别受到伤害','hp_spent':'血量减少 · 可见变化','mp_spent':'蓝量减少 · 可见变化'}
        for i,(key,title) in enumerate(titles.items()):
            f=tk.Frame(metrics,bg='white',padx=18,pady=8)
            f.grid(row=(i//2)*2,column=(i%2)*2,sticky='nsew',padx=(0,12),pady=(0,8))
            self.label(f,title,12).pack(anchor='w')
            self.metrics[key]=self.label(f,'—',METRIC_SIZE,True,ACCENT if i<2 else INK)
            self.metrics[key].pack(anchor='w',pady=(4,0))
            if key in ('damage_dealt','damage_taken'):
                canvas=tk.Canvas(f,bg='white',highlightthickness=0)
                canvas.place(relx=1,x=-round(18*self.scale),y=0,anchor='ne',width=round(96*self.scale),height=round(24*self.scale))
                self.gain_canvases[key]=canvas
                self.gain_animations[key]=CanvasGains(canvas,{key:(94,18,90,ACCENT if key=='damage_dealt' else HP,6,'e')},self.scale)
        metrics.columnconfigure(0,weight=1);metrics.columnconfigure(2,weight=1)
        tk.Frame(metrics,bg=LINE,width=1).grid(row=0,column=1,rowspan=3,sticky='ns',pady=8)
        tk.Frame(metrics,bg=LINE,height=1).grid(row=1,column=0,columnspan=3,sticky='ew')
        self.recovery=self.label(content,'血量恢复 —   ·   蓝量恢复 —',12)
        self.recovery.pack(anchor='w',pady=(0,8))
        self.state=self.label(content,'等待战斗画面',11)
        self.state.pack(anchor='w',pady=(0,4))
        self.label(content,'飘字遮挡、重叠或短暂出现会漏读；受伤与技能耗血不混算。\n血蓝减少量含受伤和技能消耗；两次采样间同时恢复与消耗可能抵消，不等于药品用量。',10).pack(anchor='w',pady=(0,14))
        table=tk.Frame(content,bg='white');table.pack(fill='both',expand=True)
        kill_page=tk.Frame(self.tabs,bg='white');self.tabs.add(kill_page,text='按怪物击杀')
        self.kill_caption=self.label(kill_page,'等待怪物血条与名称',12,True,INK)
        self.kill_caption.pack(anchor='w',pady=(12,6))
        self.label(kill_page,'按游戏名称自动分类；名称遮挡记入“未识别怪物”。\n仅统计观察到有血量再归零的怪物；秒杀、隐藏名称或特殊 Boss 血条可能漏计。',10).pack(anchor='w',pady=(0,10))
        kill_table=tk.Frame(kill_page,bg='white');kill_table.pack(fill='both',expand=True)
        style.configure('Monster.History.Treeview',rowheight=round(58*self.scale),font=(FONT,-round(12*self.scale)))
        self.kill_rows=ttk.Treeview(kill_table,columns=('name','count','last'),show='tree headings',style='Monster.History.Treeview',height=5)
        self.kill_rows.heading('#0',text='图像');self.kill_rows.column('#0',width=round(72*self.scale),stretch=False)
        self.kill_portraits=Portraits(self.kill_rows);self.kill_photos={}
        for key,title,width in [('name','怪物类型',300),('count','击杀数',120),('last','最后击杀',160)]:
            self.kill_rows.heading(key,text=title);self.kill_rows.column(key,width=width,minwidth=80,anchor='w' if key=='name' else 'center')
        self.kill_rows.pack(side='left',fill='both',expand=True)
        kill_scroll=ttk.Scrollbar(kill_table,orient='vertical',command=self.kill_rows.yview,style='History.Vertical.TScrollbar')
        kill_scroll.pack(side='right',fill='y');self.kill_rows.configure(yscrollcommand=kill_scroll.set)
        self.events=ttk.Treeview(table,columns=('time','kind','amount','detail'),show='headings',style='History.Treeview',height=8)
        for key,title,width in [('time','时间',95),('kind','项目',105),('amount','数值',110),('detail','依据',410)]:
            self.events.heading(key,text=title);self.events.column(key,width=width,minwidth=70,anchor='w' if key=='detail' else 'center')
        self.events.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(table,orient='vertical',command=self.events.yview,style='History.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
        self.events.configure(yscrollcommand=scroll.set)
        self.window.bind('<Destroy>',self.close_feedback,add='+')
        self.tabs.bind('<<NotebookTabChanged>>',lambda event:self.refresh())
        self.refresh();self.window.after(800,self.poll)

    def close_feedback(self,event):
        if event.widget is self.window:
            for animation in self.gain_animations.values():animation.close()

    def label(self,parent,text,size,bold=False,color=MUTED):
        return tk.Label(parent,text=text,bg='white',fg=color,font=(FONT,-round(size*self.scale),'bold' if bold else 'normal'),justify='left')

    def poll(self):
        if self.window.winfo_exists():self.refresh();self.window.after(800,self.poll)

    def refresh(self):
        service=self.service;days=service.store.days();selected=self.selection.get()
        self.days.configure(values=['本次',*sorted(set(days)|set(service.store.kill_days()),reverse=True)])
        totals=service.tracker.totals if selected=='本次' else days.get(selected,{k:0 for k in KINDS})
        known=service.tracker.frame_count>0 if selected=='本次' else selected in days
        enabled=selected=='本次' and service.running and self.tabs.index(self.tabs.select())==0 and self.window.state()=='normal'
        scope_changed=(id(service.tracker),selected)!=self.gain_watch.token
        changes=self.gain_watch.observe({k:totals[k] if known else None for k in self.gain_animations},
                                        (id(service.tracker),selected),enabled)
        if not enabled or scope_changed:
            for animation in self.gain_animations.values():animation.clear()
        for key,amount in changes.items():self.gain_animations[key].push(key,amount)
        for key,label in self.metrics.items():
            text=format_damage(totals[key]) if key in ('damage_dealt','damage_taken') else f'{totals[key]:,}'
            label.configure(text=text if known else '—')
            if key in ('damage_dealt','damage_taken'):
                if not known:label.configure(image='')
                elif self.damage_image_keys.get(key)!=text:
                    self.damage_photos[key]=ImageTk.PhotoImage(damage_image(text,key,round(31*self.scale),round(240*self.scale)),master=label)
                    self.damage_image_keys[key]=text
                    label.configure(image=self.damage_photos[key],compound='none')
                else:label.configure(image=self.damage_photos[key])
        self.recovery.configure(text=f"血量恢复 {totals['hp_restored']:,}   ·   蓝量恢复 {totals['mp_restored']:,}" if known else '血量恢复 —   ·   蓝量恢复 —')
        fresh=service.running and service.last_reading and time.time()-service.last_reading<2
        for name,title in [('hp','当前血量 HP'),('mp','当前蓝量 MP')]:
            value=service.tracker.vitals[name] if fresh else None
            self.vital_labels[name].configure(text=title+('   '+f'{value[0]:,} / {value[1]:,}' if value else '   —'))
            self.bars[name].configure(value=100*value[0]/value[1] if value else 0)
        self.state.configure(text=service.status+'   ·   上方血蓝为当前读数')
        kills=service.store.kills(None if selected=='本次' else selected)
        self.kill_caption.configure(text=f"{selected} · 已识别击杀 {sum(r['count'] for r in kills):,} 只" if kills else f'{selected} · 暂无已确认击杀')
        previous={self.kill_rows.item(i,'values')[0]:i for i in self.kill_rows.get_children()}
        keep_names={r['name'] for r in kills}
        for name,i in previous.items():
            if name not in keep_names:self.kill_rows.delete(i)
        for index,r in enumerate(kills):
            values=(r['name'],f"{r['count']:,}",format_time(r['last']))
            photo=self.kill_portraits.get(r['name'],round(46*self.scale));self.kill_photos[r['name']]=photo
            if r['name'] in previous:self.kill_rows.item(previous[r['name']],values=values,image=photo);self.kill_rows.move(previous[r['name']],'',index)
            else:self.kill_rows.insert('',index,values=values,image=photo)
        self.kill_photos={n:p for n,p in self.kill_photos.items() if n in keep_names}
        rows=service.store.events(None if selected=='本次' else selected)
        existing=set(self.events.get_children())
        keep={str(r['id']) for r in rows}
        for stale in existing-keep:self.events.delete(stale)
        for i,r in enumerate(rows):
            amount=format_damage(r['amount']) if r['kind'] in ('damage_dealt','damage_taken') else f"{r['amount']:,}"
            values=(format_time(r['timestamp']),LABELS[r['kind']],amount,r['detail'])
            if str(r['id']) not in existing:self.events.insert('',i,iid=str(r['id']),values=values)

    def export(self):
        selected=self.selection.get()
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.csv',initialfile=f'战斗记录-{selected}.csv',filetypes=[('CSV','*.csv')])
        if not path:return
        if self.tabs.index(self.tabs.select())==1:
            try:
                with open(path,'w',encoding='utf-8-sig',newline='') as f:
                    w=csv.writer(f);w.writerow(['时间','怪物类型','击杀数','依据'])
                    for r in self.service.store.kills(None if selected=='本次' else selected,grouped=False):
                        w.writerow([format_time(r['timestamp'],'%Y-%m-%d %H:%M:%S'),r['name'],1,r['detail']])
            except OSError as error:messagebox.showerror('导出失败',str(error),parent=self.window)
            return
        sql='SELECT * FROM combat_events WHERE '+('session_id=?' if selected=='本次' else 'day=?')+' ORDER BY timestamp,id'
        arg=self.service.store.session if selected=='本次' else selected
        try:
            with open(path,'w',encoding='utf-8-sig',newline='') as f:
                w=csv.writer(f);w.writerow(['时间','项目','数值','依据'])
                for r in self.service.store.db.execute(sql,(arg,)):
                    w.writerow([format_time(r['timestamp'],'%Y-%m-%d %H:%M:%S'),LABELS[r['kind']],r['amount'],r['detail']])
        except OSError as error:messagebox.showerror('导出失败',str(error),parent=self.window)
