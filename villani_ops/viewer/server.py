from __future__ import annotations
from pathlib import Path
import json, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse
from .adapter import build_candidate_debug, build_viewer_snapshot
from .builder import render_viewer_html

def safe_join_under(base: Path, requested: str) -> Path:
    base=Path(base).resolve(); target=(base / requested.lstrip('/')).resolve()
    if target != base and base not in target.parents:
        raise ValueError('path traversal blocked')
    return target

class ViewerServer:
    def __init__(self, runs_dir: str|Path, host='127.0.0.1', port=8765):
        self.runs_dir=Path(runs_dir).resolve(); self.host=host; self.port=port; self.httpd=None; self.thread=None
    def start(self, try_ports=8):
        last=None
        for p in range(self.port, self.port+try_ports):
            try:
                self.port=p; self.httpd=ThreadingHTTPServer((self.host,p), self._handler()); break
            except OSError as e: last=e
        if not self.httpd: raise last or OSError('viewer server failed')
        self.thread=threading.Thread(target=self.httpd.serve_forever, daemon=True); self.thread.start(); return self
    def url(self, run_id=''):
        return f'http://{self.host}:{self.port}' + (f'/runs/{run_id}' if run_id else '')
    def stop(self):
        if self.httpd: self.httpd.shutdown()
    def _run_dir(self, run_id):
        if '/' in run_id or '\\' in run_id or run_id in {'','..','.'}: return None
        try: p=safe_join_under(self.runs_dir, run_id)
        except ValueError: return None
        return p if p.exists() and p.is_dir() else None
    def _handler(self):
        outer=self
        class H(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def send_json(self, obj, code=200):
                data=json.dumps(obj, ensure_ascii=False, default=str).encode()
                try:
                    self.send_response(code); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    return
            def send_text(self, text, ctype='text/html', code=200):
                data=text.encode()
                try:
                    self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
                    return
            def notfound(self): self.send_json({'error':'not found'},404)
            def do_GET(self):
                path=unquote(urlparse(self.path).path)
                if path in {'/',''}:
                    return self.send_text('<h1>Villani Ops Viewer</h1><p>Open /runs/&lt;run_id&gt;</p>')
                parts=[p for p in path.split('/') if p]
                if len(parts)==2 and parts[0]=='runs':
                    rd=outer._run_dir(parts[1]); return self.send_text(render_viewer_html(None)) if rd else self.notfound()
                if len(parts)>=4 and parts[0]=='api' and parts[1]=='runs':
                    rd=outer._run_dir(parts[2]);
                    if not rd: return self.notfound()
                    name=parts[3]
                    if name=='snapshot': return self.send_json(build_viewer_snapshot(rd))
                    if name=='candidate' and len(parts)>=6 and parts[5]=='debug': return self.send_json(build_candidate_debug(rd, parts[4]))
                    files={'state':'state.json','events':'runtime_events.jsonl','graph':'orchestration_graph.json','usage':'usage.json'}
                    if name in files:
                        fp=rd/files[name]
                        if not fp.exists(): return self.send_json({} if name!='events' else [])
                        if name=='events':
                            rows=[]
                            for line in fp.read_text(encoding='utf-8',errors='replace').splitlines():
                                try: rows.append(json.loads(line))
                                except Exception: pass
                            return self.send_json(rows)
                        try: return self.send_json(json.loads(fp.read_text(encoding='utf-8')))
                        except Exception: return self.send_json({})
                    if name=='stream':
                        self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Cache-Control','no-store'); self.end_headers(); pos=0; fp=rd/'runtime_events.jsonl'
                        for _ in range(120):
                            if fp.exists():
                                with fp.open('rb') as f:
                                    f.seek(pos); data=f.read(); pos=f.tell()
                                for line in data.splitlines():
                                    try: self.wfile.write(b'data: '+line+b'\n\n')
                                    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError): return
                            try: self.wfile.write(b': keepalive\n\n'); self.wfile.flush()
                            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError): return
                            time.sleep(1)
                        return
                self.notfound()
        return H

def serve_forever(runs_dir: Path, port=8765, open_url: str|None=None):
    srv=ViewerServer(runs_dir, port=port).start()
    print(f'Viewer server: {srv.url()}')
    if open_url: webbrowser.open(open_url)
    try:
        while True: time.sleep(3600)
    except KeyboardInterrupt: srv.stop()
