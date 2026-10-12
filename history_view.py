"""Daily totals and session replay, backed by the persisted SQLite ledger."""
import csv
import time
from concurrent.futures import ThreadPoolExecutor
from app_time import beijing_datetime, format_time, local_day
import tkinter as tk
from tkinter import ttk,filedialog,messagebox
from minute_chart import minute_buckets
from history_reader import HistoryReader
from ui_theme import configure_history, configure_window, INK, MUTED, FONT, SURFACE, MIDNIGHT
from ui_charts import DEFAULT_RATE_VIEW,RATE_VIEWS,WINDOW_ALGORITHM,plot,rate_label,rate_plot_rows,rate_prefix,window_label,window_sample

def duration(value):
    seconds=int(value)
    return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'

class HistoryWindow:
    def __init__(self,root,store):
        self.store=store
        self.day=None
        self.session=None
        self.minute_offset=None
        self.minute_data=[]
        self._closed=False
        self._load_timer=self._redraw_timer=self._poll_timer=None
        self._request_generation=0
        self._requested_key=self._display_key=None
        self._future=None
        self._render_data=None
        self._day_summary={}
        self._reader=HistoryReader(store.path)
        self._executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='history-read')
        self.window=tk.Toplevel(root)
        self.window.title('经验观察器 · 历史记录')
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Destroy>',self._destroyed,add='+')
        self.window.attributes('-topmost',True)
        self.window.geometry('1320x1180')
        self.window.minsize(1100,1080)
        configure_window(self.window)
        style=ttk.Style()
        self.scale=max(1,root.winfo_fpixels('1i')/96)
        configure_history(style,self.scale)
        outer=ttk.Frame(self.window,padding=22)
        outer.pack(fill='both',expand=True)
        ttk.Label(outer,text='历史记录',style='HistoryTitle.TLabel').pack(anchor='w')
        self.summary=tk.StringVar()
        ttk.Label(outer,textvariable=self.summary,style='HistorySummary.TLabel').pack(anchor='w',pady=(12,6))
        ttk.Label(outer,text='按北京时间日期汇总 · 选择日期查看各次记录').pack(anchor='w',pady=(0,10))
        body=ttk.Panedwindow(outer,orient='horizontal')
        body.pack(fill='both',expand=True)
        left=ttk.Frame(body)
        right=ttk.Frame(body)
        body.add(left,weight=2)
        body.add(right,weight=3)
        self.days=ttk.Treeview(left,columns=('date','gain','duration'),show='headings',height=16,selectmode='browse',style='History.Treeview')
        for key,title,width in [('date','日期',130),('gain','经验总计',140),('duration','有效时长',110)]:
            self.days.heading(key,text=title)
            self.days.column(key,width=width,anchor='center')
        self.days.pack(side='left',fill='both',expand=True)
        scroll=ttk.Scrollbar(left,orient='vertical',command=self.days.yview)
        scroll.pack(side='right',fill='y')
        self.days.configure(yscrollcommand=scroll.set)
        self.days.bind('<<TreeviewSelect>>',self.select_day)
        self.detail=tk.StringVar(value='选择日期开始回看')
        ttk.Label(right,textvariable=self.detail,wraplength=650).pack(anchor='w',padx=14,pady=(0,6))
        runs_frame=ttk.Frame(right)
        runs_frame.pack(fill='x',padx=14)
        self.runs=ttk.Treeview(runs_frame,columns=('start','gain','duration','average'),show='headings',height=2,selectmode='browse',style='History.Treeview')
        for key,title,width in [('start','开始时间',150),('gain','当日经验',110),('duration','有效时长',100),('average','全程分均',110)]:
            self.runs.heading(key,text=title)
            self.runs.column(key,width=width,anchor='center')
        self.runs.pack(side='left',fill='x',expand=True)
        self.runs_scroll=ttk.Scrollbar(runs_frame,orient='vertical',command=self.runs.yview)
        self.runs_scroll.pack(side='right',fill='y')
        self.runs.configure(yscrollcommand=self.runs_scroll.set)
        self.runs.bind('<<TreeviewSelect>>',self.select_session)
        rate_controls=ttk.Frame(right)
        rate_controls.pack(fill='x',padx=14,pady=(10,0))
        ttk.Label(rate_controls,text='分均曲线').pack(side='left',padx=(0,8))
        self.rate_view=tk.StringVar(value=DEFAULT_RATE_VIEW)
        self.rate_selector=ttk.Combobox(rate_controls,textvariable=self.rate_view,values=list(RATE_VIEWS),
                                       state='readonly',width=12,style='History.TCombobox')
        self.rate_selector.pack(side='left')
        self.rate_selector.bind('<<ComboboxSelected>>',self.select_rate_view)
        self.rate_hint=tk.StringVar(value='观测未满窗口时按实际采样时长计算')
        ttk.Label(rate_controls,textvariable=self.rate_hint).pack(side='right')
        self.chart=tk.Canvas(right,bg=SURFACE,height=175,highlightthickness=0)
        self.chart.pack(fill='x',padx=14,pady=(6,0))
        ttk.Label(right,text='横轴为所选日期 / 记录范围；不同算法、采样段及缺失处断线。').pack(anchor='w',padx=14)
        chart_controls=ttk.Frame(right)
        chart_controls.pack(fill='x',padx=14,pady=(6,0))
        ttk.Label(chart_controls,text='自然分钟 · 收益明细').pack(side='left')
        ttk.Button(chart_controls,text='全部记录',command=self.all_sessions).pack(side='left',padx=10)
        ttk.Button(chart_controls,text='后30分',command=lambda:self.page_minutes(1)).pack(side='right')
        ttk.Button(chart_controls,text='前30分',command=lambda:self.page_minutes(-1)).pack(side='right',padx=4)
        self.minute_chart=tk.Canvas(right,bg=SURFACE,height=205,highlightthickness=0)
        self.minute_chart.pack(fill='x',padx=14,pady=(6,0))
        self.chart_hint=tk.StringVar(value='每根柱代表一个自然分钟；悬停查看精确数值。')
        ttk.Label(right,textvariable=self.chart_hint).pack(anchor='w',padx=14,pady=(0,8))
        detail_frame=ttk.Frame(right)
        detail_frame.pack(fill='both',expand=True,padx=14)
        self.timeline=ttk.Treeview(detail_frame,columns=('time','delta','realtime','current','algorithm'),show='headings',height=5,style='History.Treeview')
        for key,title,width in [('time','时间',90),('delta','获得经验',105),('realtime','1 min 分均',100),('current','当前经验',110),('algorithm','算法口径 / 状态',205)]:
            self.timeline.heading(key,text=title)
            self.timeline.column(key,width=width,anchor='center')
        self.timeline.pack(side='left',fill='both',expand=True)
        detail_scroll=ttk.Scrollbar(detail_frame,orient='vertical',command=self.timeline.yview)
        detail_scroll.pack(side='right',fill='y')
        self.timeline.configure(yscrollcommand=detail_scroll.set)
        self.chart.bind('<Configure>',lambda e:self.schedule_chart())
        self.minute_chart.bind('<Configure>',lambda e:self.schedule_chart())
        self.minute_chart.bind('<Motion>',self.hover_minute)
        footer=ttk.Frame(outer)
        footer.pack(side='bottom',fill='x',pady=(8,0),before=body)
        ttk.Button(footer,text='导出所选日期 CSV',command=self.export).pack(side='left')
        ttk.Button(footer,text='刷新',command=self.refresh).pack(side='left',padx=10)
        ttk.Label(footer,text='明细显示最近 200 帧，导出包含当日全部帧。').pack(side='right')
        self.rows=[]
        def theme_children(widget):
            for child in widget.winfo_children():
                if isinstance(child,ttk.Frame): child.configure(style='History.TFrame')
                elif isinstance(child,ttk.Label) and not child.cget('style'): child.configure(style='History.TLabel')
                elif isinstance(child,ttk.Button): child.configure(style='History.TButton')
                elif isinstance(child,ttk.Scrollbar): child.configure(style='History.Vertical.TScrollbar')
                theme_children(child)
        theme_children(self.window)
        self.refresh()
        self._poll_timer=self.window.after(5000,self.poll)

    def _destroyed(self,event):
        if event.widget is self.window:self._cleanup()

    def _cleanup(self):
        if self._closed:return
        self._closed=True
        for token in (self._load_timer,self._redraw_timer,self._poll_timer):
            if token:
                try:self.window.after_cancel(token)
                except tk.TclError:pass
        self._executor.shutdown(wait=False,cancel_futures=True)

    def close(self):
        self._cleanup()
        self.window.destroy()

    def schedule_chart(self):
        if self._closed:return
        if self._redraw_timer:self.window.after_cancel(self._redraw_timer)
        self._redraw_timer=self.window.after(60,self._redraw)

    def _redraw(self):
        self._redraw_timer=None
        self.draw_chart()

    def poll(self):
        if self.window.winfo_exists():
            self.refresh()
            self._poll_timer=self.window.after(5000,self.poll)

    def refresh(self):
        rows=self.store.days()
        self._day_summary={r['day']:r for r in rows}
        total=sum(r['gain'] for r in rows)
        today=local_day()
        today_total=next((r['gain'] for r in rows if r['day']==today),0)
        self.summary.set(f'今日总计  {today_total:,}     历史总计  {total:,}     已记录 {len(rows)} 天')
        existing=set(self.days.get_children())
        for index,row in enumerate(rows):
            day=row['day']
            values=(day,f"{row['gain']:,}",duration(row['seconds']))
            if day in existing:
                self.days.item(day,values=values)
            else:
                self.days.insert('',index,iid=day,values=values)
        if not self.day and rows:
            self.day=rows[0]['day']
            self.days.selection_set(self.day)
        if self.day:
            self.load_day()

    def select_day(self,event=None):
        selected=self.days.selection()
        if selected:
            if self.day==selected[0]:return
            if self.day!=selected[0]:
                self.session=None
                self.minute_offset=None
            self.day=selected[0]
            self.load_day()

    def load_day(self):
        row=self._day_summary.get(self.day)
        if not row:
            return
        average=row['gain']*60/row['seconds'] if row['seconds'] else 0
        self.detail.set(f"{self.day}（北京时间）  共 {row['sessions']} 次\n总计 {row['gain']:,}  ·  有效时长 {duration(row['seconds'])}  ·  全程分均 {average:,.0f}")
        entries=self.store.sessions(self.day)
        existing=set(self.runs.get_children())
        ids={r['id'] for r in entries}
        for stale in existing-ids:
            self.runs.delete(stale)
        for index,r in enumerate(entries):
            average=r['gain']*60/r['seconds'] if r['seconds'] else 0
            start=format_time(r['first'])
            if r['source']=='legacy':
                start+=' · 旧记录'
            elif r['source']=='summary':
                start+=' · 旧版汇总'
            values=(start,f"{r['gain']:,}",duration(r['seconds']),f'{average:,.0f}')
            if r['id'] in existing:
                self.runs.item(r['id'],values=values)
            else:
                self.runs.insert('',index,iid=r['id'],values=values)
        if self.session not in ids:
            self.session=None
        self.load_samples()

    def select_session(self,event=None):
        selected=self.runs.selection()
        session=selected[0] if selected else None
        if session==self.session:return
        self.session=session
        self.minute_offset=None
        self.load_samples()

    def all_sessions(self):
        self.runs.selection_remove(*self.runs.selection())
        self.session=None
        self.minute_offset=None
        self.load_samples()

    def load_samples(self):
        if self._closed or not self.day:return
        key=(self.day,self.session,self.selected_window(),self._day_summary.get(self.day,{}).get('last_id'))
        if key==self._requested_key or (key==self._display_key and self._requested_key is None):return
        self._requested_key=key
        self._request_generation+=1
        generation=self._request_generation
        if self._future:self._future.cancel()
        if self._load_timer:self.window.after_cancel(self._load_timer)
        self.rate_hint.set('正在读取历史记录……')
        if self._display_key is None or key[:3]!=self._display_key[:3]:
            self.chart.delete('all');self.minute_chart.delete('all')
            self.timeline.delete(*self.timeline.get_children())
            self.chart.create_text(20,25,text='正在读取所选记录……',anchor='nw',fill=MUTED)
        self._future=self._executor.submit(self._reader.read,self.day,self.session,self.selected_window())
        self._load_timer=self.window.after(20,lambda:self._finish_load(generation,key))

    def _finish_load(self,generation,key):
        self._load_timer=None
        if self._closed or generation!=self._request_generation:return
        if not self._future.done():
            self._load_timer=self.window.after(20,lambda:self._finish_load(generation,key))
            return
        self._requested_key=None
        try:
            data=self._future.result()
        except Exception:
            self.rate_hint.set('读取失败，请点击刷新重试。')
            return
        self.rows=data['rows']
        self.minute_data=data['minute_data']
        self._render_data=data
        self._display_key=key
        self.select_rate_view(prepared=True)

    def selected_window(self):
        return RATE_VIEWS[self.rate_view.get()]

    def select_rate_view(self,event=None,*,prepared=False):
        if not prepared and hasattr(self,'_executor'):
            self.load_samples()
            return
        window=self.selected_window()
        hint='速度趋势预热中；非整分钟收益' if window is None else '观测未满窗口时按实际采样时长计算'
        data=getattr(self,'_render_data',None)
        if (data['has_gaps'] if data else any(bool(r.get('rate_has_gaps')) if window is None else window_sample(r,window)['has_gaps'] for r in self.rows)):
            hint+='；≥ 收益下限，升级尾段未计入'
        self.rate_hint.set(hint)
        self.timeline.heading('realtime',text='速度趋势' if window is None else f'{window_label(window)} 分均')
        self.timeline.delete(*self.timeline.get_children())
        for r in reversed(self.rows[-200:]):
            gain=f"汇总 {r['gain']:,}" if r['status']=='legacy_summary' else f"+{r['gain']:,}"
            if r['status']=='auto_level_gap':gain+='（升级·尾段未知）'
            elif r['status']=='auto_level':gain+='（升级）'
            elif r['status']=='confirmed_level':gain+='（升级补记）'
            elif r['status']=='gap':gain+='（断采补记）'
            sample=window_sample(r,window) if window is not None else dict(value=r['realtime'],state=r.get('rate_state'),has_gaps=bool(r.get('rate_has_gaps')))
            state={'unavailable':'无可用值','stale':'读数中断','warming':'预热','ready':'就绪','paused':'已暂停','legacy':'覆盖未知'}.get(sample['state'])
            if window is None:
                label=rate_label(r,compact=True)
            elif r.get('window_algorithm')==WINDOW_ALGORITHM:
                coverage=sample['observed_seconds']
                if sample['state']=='ready':
                    label='满窗口'
                    state=None
                else:
                    observed=f'{coverage:.1f}'.rstrip('0').rstrip('.') if coverage is not None else None
                    label=f'观测 {observed}秒' if observed is not None else '有效观测窗口'
            else:
                label='旧60秒口径' if sample['value'] is not None else '旧记录无此窗口'
                if sample['value'] is None:
                    state=None
            label+=(f' · {state}' if state else '')
            if sample['has_gaps']:
                label+=' · 升级尾段未计入'
            value=sample['value']
            formatted='—' if value is None else rate_prefix(sample)+f'{value:,.0f}'
            self.timeline.insert('','end',values=(format_time(r['timestamp']),gain,formatted,'—' if r['current_exp'] is None else f"{r['current_exp']:,}",label))
        self.draw_chart()

    def draw_chart(self):
        if hasattr(self,'_executor') and (self._render_data is None or self._requested_key is not None):return
        c=self.chart
        c.delete('all')
        self.minute_chart.delete('all')
        self.draw_minutes()
        width=max(300,c.winfo_width())
        window=self.selected_window()
        data=getattr(self,'_render_data',None)
        if data:
            caption=data['caption']
            title='速度趋势曲线' if window is None else f'{window_label(window)} 分均曲线'
        elif window is None:
            labels=list(dict.fromkeys(rate_label(r) for r in self.rows if r['realtime'] is not None))
            caption=labels[0] if len(labels)==1 else ('混合口径（见明细）' if labels else '')
            title='速度趋势曲线'
        else:
            versions={r.get('window_algorithm') or 'legacy' for r in self.rows if window_sample(r,window)['value'] is not None}
            caption='混合版本（见明细）' if len(versions)>1 else ('旧60秒口径 · 覆盖未知' if versions=={'legacy'} else '有效观测窗口')
            title=f'{window_label(window)} 分均曲线'
        c.create_text(12,8,text=title+(f' · {caption}' if caption else ''),fill=INK,anchor='nw',font=(FONT,-round(14*self.scale),'bold'))
        rows=data['plot_rows'] if data else rate_plot_rows(self.rows,window_seconds=window)
        if not rows:
            c.create_text(width/2,90,text='所选口径没有已保存的有效值；旧记录不会补算窗口。',fill=MUTED,width=width-30)
            return
        chart_scale=self.scale
        plot(c,(10/chart_scale,35/chart_scale,(width-20)/chart_scale,138/chart_scale),
             rows,
             scale=chart_scale,time_range=(self.rows[0]['timestamp'],self.rows[-1]['timestamp']),
             max_gap=None if data else 10,palette=MIDNIGHT)

    def page_minutes(self,direction):
        if getattr(self,'_requested_key',None) is not None:return
        count=len(self.minute_data)
        last=max(0,count-30)
        start=last if self.minute_offset is None else self.minute_offset
        start=max(0,min(last,start+direction*30))
        self.minute_offset=None if start==last else start
        self.minute_chart.delete('all')
        self.draw_minutes()

    def draw_minutes(self):
        c=self.minute_chart
        width=max(300,c.winfo_width())
        if not getattr(self,'_render_data',None):self.minute_data=minute_buckets(self.rows)
        self.chart_hint.set('每根柱代表一个自然分钟；悬停查看数值，亮蓝色为当前分钟。')
        if not self.minute_data:
            c.create_text(width/2,90,text='暂无分钟明细；只有汇总的旧记录不拆分估算。',fill=MUTED,width=width-30)
            return
        last=max(0,len(self.minute_data)-30)
        start=last if self.minute_offset is None else min(self.minute_offset,last)
        rows=self.minute_data[start:start+30]
        subtotal=sum(r['gain'] for r in rows)
        caption=f"每分钟获得经验   ·   此段总计 {subtotal:,}"
        if any(r.get('status')=='legacy_summary' for r in self.rows):
            caption+='（不含旧版汇总）'
        c.create_text(12,8,text=caption,fill=INK,anchor='nw',font=(FONT,-round(14*self.scale),'bold'))
        peak=max(1,max(r['gain'] for r in rows))
        left,right,top,bottom=round(60*self.scale),width-12,43,170
        for ratio in (0,.5,1):
            y=bottom-(bottom-top)*ratio
            c.create_line(left,y,right,y,fill=MIDNIGHT['line'],dash=(3,4))
            c.create_text(left-7,y,text=f'{peak*ratio:,.0f}',fill=MUTED,anchor='e',font=(FONT,-round(9*self.scale)))
        step=(right-left)/len(rows)
        self.minute_hit=(left,right,top,bottom,rows)
        current=int(time.time()//60)*60
        for index,row in enumerate(rows):
            x=left+index*step
            height=(bottom-top)*row['gain']/peak
            color=MIDNIGHT['accent'] if row['timestamp']==current else MIDNIGHT['bar']
            tag=f'minute_{index}'
            if row['observed']:
                c.create_rectangle(x+2,bottom-max(2,height),x+max(3,step-2),bottom,fill=color,outline='',tags=(tag,))
            else:
                c.create_line(x+2,bottom-2,x+max(3,step-2),bottom-2,fill=MUTED,dash=(2,2),tags=(tag,))
            # A transparent hit area makes zero-gain minutes easy to inspect too.
            c.create_rectangle(x,top,x+step,bottom,fill='',outline='',tags=(tag,))
            label=format_time(row['timestamp'],'%H:%M')
            suffix='（本分钟尚未结束）' if row['timestamp']==current else ''
            detail=f"{label}   +{row['gain']:,} 经验 {suffix}" if row['observed'] else f'{label}   未记录，不计作零收益'
            c.tag_bind(tag,'<Enter>',lambda e,text=detail:self.chart_hint.set(text))
            last_index=len(rows)-1
            spacing=max(2,int(60/max(step,1))+1)
            if (index%max(1,(len(rows)+4)//5)==0 and index<=last_index-spacing) or index==last_index:
                c.create_text(x+step/2,188,text=label,fill=MUTED,font=(FONT,-round(9*self.scale)))

    def hover_minute(self,event):
        if getattr(self,'_requested_key',None) is not None:return
        if not self.minute_data or not hasattr(self,'minute_hit'):
            return
        left,right,top,bottom,rows=self.minute_hit
        if not(left<=event.x<right and top<=event.y<=bottom):
            return
        row=rows[min(len(rows)-1,int((event.x-left)/(right-left)*len(rows)))]
        label=format_time(row['timestamp'],'%H:%M')
        suffix='（本分钟尚未结束）' if row['timestamp']==int(time.time()//60)*60 else ''
        self.chart_hint.set(f"{label}   +{row['gain']:,} 经验 {suffix}" if row['observed'] else f'{label}   未记录，不计作零收益')

    def export(self):
        if not self.day:
            return
        path=filedialog.asksaveasfilename(parent=self.window,defaultextension='.csv',initialfile=f'经验记录-{self.day}.csv',filetypes=[('CSV','*.csv')])
        if path:
            try:
                rows=self.store.samples(self.day)
                with open(path,'w',encoding='utf-8-sig',newline='') as file:
                    writer=csv.writer(file)
                    writer.writerow(['时间','会话','获得经验','当前经验','速度趋势（按算法列）','全程分均','全程时均','本次累计','有效秒数','状态','分均算法','半衰期秒数','分均状态','观测秒数','1min分均（按窗口算法）','分均采样段','2min分均（按窗口算法）','5min分均（按窗口算法）','10min分均（按窗口算法）','30min分均（按窗口算法）','窗口算法版本','趋势收益有缺口（1是/0否）','窗口缺口位掩码（1/2/5/10/30min=1/2/4/8/16）'])
                    for r in rows:
                        writer.writerow([beijing_datetime(r['timestamp']).isoformat(),r['session_id'],r['gain'],r['current_exp'],r['realtime'],r['average'],r['hourly'],r['total'],r['elapsed'],r['status'],r.get('rate_algorithm') or 'rolling60_v1',r.get('rate_half_life'),r.get('rate_state'),r.get('rate_observed_seconds'),r.get('rolling60'),r.get('rate_segment'),r.get('rolling120'),r.get('rolling300'),r.get('rolling600'),r.get('rolling1800'),r.get('window_algorithm'),int(bool(r.get('rate_has_gaps'))),r.get('window_gap_mask') or 0])
            except OSError as error:
                messagebox.showerror('导出失败',str(error),parent=self.window)
