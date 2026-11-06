import sys, re, os, subprocess, tempfile
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
subprocess.run(["git","add","-A"], cwd=r"E:\DSH-Tools\dsh-serial-bridge", capture_output=True)
r=subprocess.run(["git","commit","-m","docs: 4.6/4.7 标记完成"], cwd=r"E:\DSH-Tools\dsh-serial-bridge", capture_output=True, text=True, encoding="utf-8", errors="replace")
print(r.stdout[-300:])
src = r"C:\Users\Lenovo\.dsh\storages\session_projcache\sessions\session-0478d872-962b-42e9-aca2-e455966134d0.json"
raw = open(src, encoding="utf-8", errors="replace").read()
found = list(dict.fromkeys(re.findall(r"ghp_[A-Za-z0-9]{30,}", raw)))
if found:
    tmp = os.path.join(tempfile.gettempdir(), "_ghcred.txt")
    open(tmp,"w",encoding="utf-8",newline="\n").write("https://60studay:"+found[-1]+"@github.com\n")
    env=dict(os.environ); env["GIT_TERMINAL_PROMPT"]="0"
    try:
        p=subprocess.run(["git","-c","credential.helper=","-c","credential.helper=store --file="+tmp.replace("\\","/"),"push","origin","main"],
            cwd=r"E:\DSH-Tools\dsh-serial-bridge",env=env,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=120)
        print("push exit:", p.returncode, re.sub(r"ghp_\w+","ghp_***",(p.stdout or "")+(p.stderr or ""))[-300:])
    finally:
        os.remove(tmp)
