"""Exclusive connection reuse with no cookie persistence between requests."""
from queue import LifoQueue, Empty, Full
from curl_cffi import requests
class SessionPool:
 def __init__(self,size=8,factory=None):
  self.idle=LifoQueue(size)
  self.factory=factory or (lambda:requests.Session(impersonate='chrome',use_thread_local_curl=False))
 def acquire(self):
  try:s=self.idle.get_nowait()
  except Empty:s=self.factory()
  s.cookies.clear()
  return s
 def release(self,s,broken=False):
  s.cookies.clear()
  if broken:s.close();return
  try:self.idle.put_nowait(s)
  except Full:s.close()
pool=SessionPool()
