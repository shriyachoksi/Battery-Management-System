"""Log training data from the ESP32 running smart_bms_ml.ino.

    python ml/collect_data.py COM5                       # Windows
    python ml/collect_data.py /dev/ttyUSB0 --discharge   # also start a discharge

Sends 'L' to switch the firmware's CSV logging on, writes every LOG line to
ml/data/real/<timestamp>.csv, and switches logging off again on Ctrl+C.
Other serial output (faults, warnings) is echoed to the console.

For SoC labels, train_models.py needs full discharges: charge the cell until
the TP4056 finishes, let it rest for at least 10 minutes, then discharge
('D', or --discharge) until the firmware's undervoltage cutoff stops it. A few
such cycles, ideally at different temperatures, are enough to retrain.
"""

import argparse
import csv
import time
from datetime import datetime
from pathlib import Path

import serial

OUT_DIR = Path(__file__).resolve().parent / "data" / "real"
COLUMNS = ["t_ms", "voltage", "current_mA", "temp_c", "load", "charging_v1", "soc_v1",
           "soc_ml", "soc_fused", "anomaly_score", "anomaly", "ml_ready"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="serial port of the ESP32, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--discharge", action="store_true", help="send 'D' once logging has started")
    ap.add_argument("--out", type=Path, help="output CSV (default: ml/data/real/<timestamp>.csv)")
    args = ap.parse_args()

    out = args.out or OUT_DIR / f"{datetime.now():%Y%m%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)

    with serial.Serial(args.port, args.baud, timeout=1) as port, open(out, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        time.sleep(2)  # opening the port resets most ESP32 boards
        port.reset_input_buffer()
        port.write(b"L")
        if args.discharge:
            port.write(b"D")
        print(f"Logging to {out} - Ctrl+C to stop")

        rows = 0
        try:
            while True:
                line = port.readline().decode("ascii", errors="replace").strip()
                if not line:
                    continue
                if line.startswith("LOG,"):
                    fields = line.split(",")[1:]
                    if len(fields) == len(COLUMNS):
                        writer.writerow(fields)
                        rows += 1
                        if rows % 60 == 0:
                            f.flush()
                            print(f"  {rows} samples  V={fields[1]}  I={fields[2]} mA  T={fields[3]} C  SoC={fields[8]} %")
                elif line == "Logging off.":
                    # The firmware toggles on 'L': it was already logging before we started.
                    port.write(b"L")
                elif not line.startswith("LOGHDR"):
                    print(f"  [esp32] {line}")
        except KeyboardInterrupt:
            pass
        finally:
            port.write(b"L")
            print(f"\nSaved {rows} samples to {out}")


if __name__ == "__main__":
    main()
