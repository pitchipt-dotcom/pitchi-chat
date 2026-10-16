"""Portable release tests with private HOME/runtime and no inherited credentials."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="release-test-") as temporary:
    home=Path(temporary)
    (home/"runtime").mkdir(mode=0o700)
    env={"PATH":os.environ.get("PATH","/usr/bin:/bin"),"LANG":"C.UTF-8",
         "HOME":temporary,"XDG_RUNTIME_DIR":str(home/"runtime"),
         "PYTHONDONTWRITEBYTECODE":"1","PYTHONPATH":str(root/"src" if (root/"src").is_dir() else root)}
    program="""import sys,ipaddress

def guard(event,args):
    if event=='socket.getaddrinfo' and args[0] not in ('localhost','127.0.0.1','::1',None):
        raise PermissionError('external network disabled during release tests')
    if event in ('socket.connect','socket.sendto') and len(args)>1 and isinstance(args[1],tuple):
        host=args[1][0]
        try:local=ipaddress.ip_address(host).is_loopback
        except ValueError:local=host=='localhost'
        if not local:raise PermissionError('external network disabled during release tests')
sys.addaudithook(guard)
import pytest
raise SystemExit(pytest.main(['-q']))
"""
    result=subprocess.run([sys.executable,"-B","-c",program],cwd=root,env=env)
    raise SystemExit(result.returncode)
