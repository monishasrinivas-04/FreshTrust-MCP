import sqlite3, hashlib, json, time
from pathlib import Path
from config.settings import settings

class FileCache:
    def __init__(self,path=None):
        self.path=path or settings.cache_path
        Path(self.path).parent.mkdir(parents=True,exist_ok=True)
        with self._connect() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS files(
            cache_key TEXT PRIMARY KEY, repo TEXT, path TEXT, content TEXT,
            content_hash TEXT, commit_sha TEXT, fetched_epoch REAL)""")
    def _connect(self): return sqlite3.connect(self.path,timeout=15)
    def _key(self,repo,path,revision):
        return hashlib.sha256(f"{repo}:{path}:{revision or 'unknown'}".encode()).hexdigest()
    def get(self,repo,path,revision=None,allow_expired=False):
        key=self._key(repo,path,revision)
        with self._connect() as c:
            row=c.execute("SELECT content,content_hash,commit_sha,fetched_epoch FROM files WHERE cache_key=?",(key,)).fetchone()
        if not row: return None
        age=time.time()-row[3]
        if age<0 or (not allow_expired and age>settings.cache_ttl_seconds): return None
        return {"path":path,"content":row[0],"content_hash":row[1],"commit_sha":row[2],
                "fetched_epoch":row[3],"cache_age_seconds":age}
    def list_repo(self, repo):
        with self._connect() as c:
            rows=c.execute("SELECT path,content,content_hash,commit_sha,fetched_epoch FROM files WHERE repo=?",(repo,)).fetchall()
        return [{"path":r[0],"content":r[1],"content_hash":r[2],"commit_sha":r[3],"fetched_epoch":r[4]} for r in rows]
    def put(self,repo,path,content,revision=None,commit_sha=None):
        digest=hashlib.sha256(content.encode()).hexdigest()
        key=self._key(repo,path,revision)
        with self._connect() as c:
            c.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?)",
                      (key,repo,path,content,digest,commit_sha,time.time()))
        return digest
