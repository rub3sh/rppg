"""Reset the webcam's V4L2 controls back to automatic.

V4L2 control state lives on the device, not in the process, so a manual
exposure set by one program persists for every program afterwards - including
the browser. This puts exposure, white balance and gain back on auto.
"""
import cv2, sys, time

dev = int(sys.argv[1]) if len(sys.argv) > 1 else 0
cap = cv2.VideoCapture(dev)
if not cap.isOpened():
    raise SystemExit(f'/dev/video{dev} is busy - close the browser tab using the camera')

print('before:',
      'auto_exp', cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
      '| exposure', cap.get(cv2.CAP_PROP_EXPOSURE),
      '| auto_wb', cap.get(cv2.CAP_PROP_AUTO_WB))

cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)      # 3 = aperture priority (auto)
cap.set(cv2.CAP_PROP_AUTO_WB, 1)            # white balance back on auto
cap.set(cv2.CAP_PROP_BRIGHTNESS, 128)
cap.set(cv2.CAP_PROP_CONTRAST, 32)
cap.set(cv2.CAP_PROP_SATURATION, 64)

for _ in range(40):                          # let auto-exposure re-converge
    cap.read()
time.sleep(1.0)
ok, f = cap.read()

print('after :',
      'auto_exp', cap.get(cv2.CAP_PROP_AUTO_EXPOSURE),
      '| exposure', cap.get(cv2.CAP_PROP_EXPOSURE),
      '| auto_wb', cap.get(cv2.CAP_PROP_AUTO_WB))
print(f'brightness now {f.mean():.1f}/255  (was ~1 when stuck in manual mode)')
cap.release()
