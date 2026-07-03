from __future__ import annotations
import fnmatch, json, os
from pathlib import Path
from typing import Any
from .errors import VerifierToolError

SECRET_NAMES={'.env','id_rsa','id_ed25519'}
SECRET_SUFFIXES=('.pem','.key')
IGNORE={'.git','node_modules','dist','build','coverage','.venv','venv','__pycache__'}
TRANSCRIPTS=('model_responses.jsonl','turns.jsonl','events.jsonl')
DIFFS=('patches.jsonl','tool_calls.jsonl')

def _is_binary(p:Path):
    try: p.open('rb').read(4096).decode('utf-8'); return False
    except UnicodeDecodeError: return True

def _bounded(s:str,n:int): return s if len(s)<=n else s[:n]+f"\n...[truncated {len(s)-n} chars]"
def _secret(rel:str):
    name=Path(rel).name
    return name in SECRET_NAMES or name.startswith('.env.') or name.endswith(SECRET_SUFFIXES)
def _safe(root:Path, rel:str, block_secret=True):
    rel_path=Path(rel or '')
    if not rel: raise VerifierToolError('unsafe path')
    if rel_path.is_absolute(): raise VerifierToolError('absolute paths are blocked')
    if '..' in rel_path.parts: raise VerifierToolError('path traversal is blocked')
    if block_secret and _secret(rel): raise VerifierToolError('secret-looking path blocked')
    root_resolved=root.resolve()
    candidate=root/rel_path
    current=root
    for part in rel_path.parts:
        current=current/part
        if current.exists() and current.is_symlink(): raise VerifierToolError('symlinks are blocked')
    resolved=candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise VerifierToolError('path escapes allowed root')
    if not resolved.exists() or not resolved.is_file(): raise VerifierToolError('file not found')
    if _is_binary(resolved): raise VerifierToolError('binary file blocked')
    return resolved

class VerifierTools:
    def __init__(self, run, repo_dir=None, max_chars=12000, max_lines=160):
        self.run=run; self.debug=Path(run.debugDir).resolve(); self.repo=Path(repo_dir).resolve() if repo_dir else None; self.max_chars=max_chars; self.max_lines=max_lines
    def _read_lines(self,p,start=1,maxLines=None):
        maxLines=min(int(maxLines or self.max_lines), self.max_lines); start=max(1,int(start or 1)); out=[]
        for i,line in enumerate(p.read_text(errors='replace').splitlines(),1):
            if i<start: continue
            if len(out)>=maxLines: break
            out.append({'line':i,'text':line})
        return {'path':str(p.name),'startLine':start,'lines':out,'truncated':len(out)>=maxLines}
    def list_debug_files(self, **args):
        files=[]
        for p in sorted(self.debug.iterdir()):
            if p.is_file() and not p.name.startswith('.') and not _is_binary(p) and not _secret(p.name): files.append(p.name)
        return {'files':files}
    def _path_arg(self,path=None,filename=None,**args):
        p=path or filename
        if not p: raise VerifierToolError('path or filename is required')
        return p
    def read_debug_file(self,path=None,filename=None,startLine=1,maxLines=None,**args): return self._read_lines(_safe(self.debug,self._path_arg(path,filename),True),startLine,maxLines)
    def search_debug_file(self,path=None,filename=None,query='',limit=10,**args): return self._search_file(_safe(self.debug,self._path_arg(path,filename),True),query,limit)
    def _search_file(self,p,query,limit):
        q=str(query).lower(); m=[]
        for i,line in enumerate(p.read_text(errors='replace').splitlines(),1):
            if q in line.lower():
                m.append({'line':i,'preview':_bounded(line,500)})
                if len(m)>=int(limit): break
        return {'path':p.name,'matches':m}
    def search_commands(self,query='',exitCode=None,limit=10,**args):
        q=str(query).lower(); out=[]
        for c in self.run.commands:
            blob=' '.join(str(x or '') for x in [c.command,c.stdout,c.stderr,c.cwd]).lower()
            if (not q or q in blob) and (exitCode is None or c.exitCode==exitCode):
                out.append({'commandId':c.toolCallId or f'cmd-{c.index}','index':c.index,'timestamp':c.ts,'cwd':c.cwd,'command':c.command,'exitCode':c.exitCode,'stdoutPreview':_bounded(c.stdout or '',700),'stderrPreview':_bounded(c.stderr or '',700)})
                if len(out)>=int(limit): break
        return {'matches':out}
    def read_command(self,commandId=None,index=None,**args):
        c=next((x for x in self.run.commands if (commandId and (x.toolCallId==commandId or f'cmd-{x.index}'==commandId)) or (index is not None and x.index==int(index))),None)
        if not c: raise VerifierToolError('command not found')
        return {'commandId':c.toolCallId or f'cmd-{c.index}','index':c.index,'timestamp':c.ts,'cwd':c.cwd,'command':c.command,'exitCode':c.exitCode,'stdout':_bounded(c.stdout or '',self.max_chars//2),'stderr':_bounded(c.stderr or '',self.max_chars//2),'truncated':c.truncated or len(c.stdout or '')+len(c.stderr or '')>self.max_chars}
    def search_tool_calls(self,query='',limit=10,**args):
        q=str(query).lower(); out=[]
        for t in self.run.toolCalls:
            blob=json.dumps(t.raw or {},default=str).lower()
            if q in blob:
                out.append({'toolCallId':t.toolCallId or f'tool-{t.index}','index':t.index,'toolName':t.toolName,'status':t.status,'preview':_bounded(blob,900)})
                if len(out)>=int(limit): break
        return {'matches':out}
    def read_tool_call(self,toolCallId=None,index=None,**args):
        t=next((x for x in self.run.toolCalls if (toolCallId and (x.toolCallId==toolCallId or f'tool-{x.index}'==toolCallId)) or (index is not None and x.index==int(index))),None)
        if not t: raise VerifierToolError('tool call not found')
        return {'toolCallId':t.toolCallId or f'tool-{t.index}','index':t.index,'toolName':t.toolName,'status':t.status,'args':_bounded(json.dumps(t.args,default=str),self.max_chars//3),'resultSummary':_bounded(json.dumps(t.resultSummary,default=str),self.max_chars//3),'error':t.error,'startedAt':t.startedAt,'endedAt':t.endedAt}
    def search_transcript(self,query,limit=10,**args):
        out=[]
        for name in TRANSCRIPTS:
            p=self.debug/name
            if p.exists(): out += [{**m,'source':name} for m in self._search_file(p,query,max(1,int(limit)-len(out)))['matches']]
            if len(out)>=int(limit): break
        return {'matches':out[:int(limit)]}
    def _repo_unavail(self): return {'available':False,'reason':'repo dir unavailable'}
    def list_repo_files(self,glob='**/*',limit=100,**args):
        if not self.repo or not self.repo.is_dir(): return self._repo_unavail()
        files=[]
        for p in self.repo.rglob('*'):
            rel=str(p.relative_to(self.repo)); parts=set(Path(rel).parts)
            if p.is_file() and not(parts&IGNORE) and fnmatch.fnmatch(rel,glob) and not _secret(rel) and not _is_binary(p): files.append(rel)
            if len(files)>=int(limit): break
        return {'available':True,'files':files}
    def read_repo_file(self,path=None,filename=None,startLine=1,maxLines=None,**args):
        if not self.repo or not self.repo.is_dir(): return self._repo_unavail()
        return self._read_lines(_safe(self.repo,self._path_arg(path,filename),True),startLine,maxLines)
    def search_repo(self,query,glob='**/*',limit=20,**args):
        if not self.repo or not self.repo.is_dir(): return self._repo_unavail()
        out=[]
        for rel in self.list_repo_files(glob,limit=500).get('files',[]):
            try: ms=self._search_file(_safe(self.repo,rel,True),query,2)['matches']
            except VerifierToolError: continue
            for m in ms: out.append({'path':rel,**m})
            if len(out)>=int(limit): break
        return {'available':True,'matches':out[:int(limit)]}
    def search_diff(self,query,limit=20,**args):
        out=[]
        for name in DIFFS:
            p=self.debug/name
            if p.exists(): out += [{**m,'source':name} for m in self._search_file(p,query,max(1,int(limit)-len(out)))['matches']]
        return {'matches':out[:int(limit)]}
    def read_diff(self,path=None,filename=None,startLine=1,maxLines=None,**args):
        rel=str(self._path_arg(path,filename)); closest=[]
        for name in DIFFS:
            p=self.debug/name
            if p.exists() and rel in p.read_text(errors='replace'):
                return self._read_lines(p,startLine,maxLines)
            if p.exists():
                for line in p.read_text(errors='replace').splitlines():
                    if Path(rel).name and Path(rel).name in line: closest.append(line[:300])
        return {'available':False,'closestMatches':closest[:20]}
    def dispatch(self,name,args):
        if not hasattr(self,name): raise VerifierToolError('unknown tool')
        res=getattr(self,name)(**(args or {})); return json.dumps(res,default=str)[:self.max_chars]
