"""Wait for a USB-tethering interface, then regen the cert and restart the server on it."""
import subprocess, time, re, os, signal, sys

def tether_ip():
    out = subprocess.run(['ip','-4','-o','addr','show','scope','global'],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        m = re.search(r'^\d+:\s+(\S+)\s+inet\s+([\d.]+)/', line)
        if not m: continue
        dev, ip = m.groups()
        if dev.startswith(('enx','usb')) or (dev.startswith('en') and ip.startswith('10.')):
            return dev, ip
    return None, None

print('waiting for USB tethering (enable it on the phone)...', flush=True)
for _ in range(180):
    dev, ip = tether_ip()
    if ip:
        print(f'\nTETHER UP: {dev} -> {ip}', flush=True)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
            '-keyout','key.pem','-out','cert.pem','-days','365','-subj',f'/CN={ip}',
            '-addext',f'subjectAltName=IP:{ip},IP:192.168.29.188,IP:127.0.0.1,DNS:localhost'],
            capture_output=True)
        # stop whatever holds the ports
        ss = subprocess.run(['ss','-tlnp'], capture_output=True, text=True).stdout
        for pid in set(re.findall(r'pid=(\d+)', '\n'.join(
                l for l in ss.splitlines() if ':8443' in l or ':8080' in l))):
            try: os.kill(int(pid), signal.SIGTERM)
            except Exception: pass
        time.sleep(1)
        for script, arg in (('server.py', ip), ('probe.py', None)):
            cmd = ['.venv/bin/python', script] + ([arg] if arg else [])
            subprocess.Popen(cmd, stdout=open(f'/tmp/{script}.log','w'),
                             stderr=subprocess.STDOUT, start_new_session=True)
        time.sleep(2)
        print(f'\n  OPEN ON PHONE:  https://{ip}:8443', flush=True)
        print(f'  test page:      http://{ip}:8080\n', flush=True)
        sys.exit(0)
    time.sleep(1)
print('timed out after 3 minutes - tethering never came up', flush=True)
