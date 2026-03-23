"""
rssi_detector.py — WiFi RSSI Human Presence Detector
Uses laptop WiFi card in monitor mode to detect human presence
via RSSI variance from nearby packets — no ESP32 needed.

Usage:
    sudo python rssi_detector.py --iface wlp0s20f3
    python rssi_detector.py --sim        # no hardware, no sudo needed

On exit (Ctrl+C): automatically restores managed mode + NetworkManager
"""

import sys
import os
import argparse
import threading
import time
import subprocess
import signal
import atexit

import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.animation as animation
from collections import deque

WINDOW       = 300
FFT_WINDOW   = 128
BASELINE_LEN = 60
Z_MOVING     = 3.5
Z_STATIONARY = 1.8
PACKET_RATE  = 20.0

BG    = '#0d1117'; PANEL = '#161b22'; BORDER = '#30363d'
BLUE  = '#58a6ff'; RED   = '#f85149'; ORANGE = '#ffa657'
GREEN = '#3fb950'; DIM   = '#8b949e'; WHITE  = '#e6edf3'

lock          = threading.Lock()
rssi_window   = deque([0.0] * WINDOW, maxlen=WINDOW)
z_scores      = deque([0.0] * WINDOW, maxlen=WINDOW)
var_scores    = deque([0.0] * WINDOW, maxlen=WINDOW)
packet_count  = [0]
status_text   = ["Calibrating..."]
dominant_freq = [0.0]
baseline_buf  = []
iface_saved   = [None]

def run(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)

def set_monitor_mode(iface):
    print(f"[*] Stopping NetworkManager...")
    run("systemctl stop NetworkManager")
    time.sleep(1)
    print(f"[*] Setting {iface} to monitor mode...")
    run(f"ip link set {iface} down")
    run(f"iw dev {iface} set type monitor")
    run(f"ip link set {iface} up")
    time.sleep(2)
    r = run(f"ip link show {iface}")
    print(f"[*] Interface state: {'UP' if 'UP' in r.stdout else 'retrying...'}")
    if "UP" not in r.stdout:
        run(f"ip link set {iface} up")
        time.sleep(2)
    print(f"[*] Monitor mode ready")

def set_managed_mode(iface):
    print(f"\n[*] Restoring {iface} to managed mode...")
    run(f"ip link set {iface} down")
    run(f"iw dev {iface} set type managed")
    run(f"ip link set {iface} up")
    time.sleep(1)
    run("systemctl start NetworkManager")
    print(f"[*] NetworkManager restarted — WiFi reconnecting...")

def cleanup():
    if iface_saved[0]:
        set_managed_mode(iface_saved[0])
        iface_saved[0] = None

def signal_handler(sig, frame):
    print("\n[*] Ctrl+C caught — restoring WiFi...")
    cleanup()
    sys.exit(0)

def process_rssi(rssi_val):
    global baseline_buf
    with lock:
        packet_count[0] += 1
        rssi_window.append(rssi_val)
        arr = np.array(rssi_window)

        if len(baseline_buf) < BASELINE_LEN:
            baseline_buf.append(rssi_val)
            z_scores.append(0.0)
            var_scores.append(0.0)
            status_text[0] = f"Calibrating... ({len(baseline_buf)}/{BASELINE_LEN})"
            return

        base_mean = np.mean(baseline_buf)
        base_std  = np.std(baseline_buf) + 0.001
        z = abs(np.mean(arr[-15:]) - base_mean) / base_std
        z_scores.append(min(z, 8.0))

        var_score = (np.std(arr[-20:]) if len(arr) >= 20 else 0.0) / (np.std(baseline_buf) + 0.001)
        var_scores.append(min(var_score, 8.0))

        if len(arr) >= FFT_WINDOW:
            seg  = arr[-FFT_WINDOW:] - np.mean(arr[-FFT_WINDOW:])
            mag  = np.abs(np.fft.rfft(seg * np.hanning(FFT_WINDOW)))
            frq  = np.fft.rfftfreq(FFT_WINDOW, d=1.0/PACKET_RATE)
            mask = (frq >= 0.05) & (frq <= 3.0)
            if mask.any():
                dominant_freq[0] = frq[mask][np.argmax(mag[mask])]

        if z < 0.8 and var_score < 1.2:
            baseline_buf.append(rssi_val)
            if len(baseline_buf) > 120:
                baseline_buf.pop(0)

        if z > Z_MOVING or var_score > 3.5:
            status_text[0] = "HUMAN DETECTED — MOVING"
        elif z > Z_STATIONARY or var_score > 2.0:
            status_text[0] = "HUMAN DETECTED — STATIONARY"
        else:
            status_text[0] = "No human detected"

def sniff_packets(iface):
    from scapy.all import sniff, RadioTap
    print(f"[*] Sniffing on {iface}...")

    def handler(pkt):
        try:
            if pkt.haslayer(RadioTap):
                rssi = float(pkt[RadioTap].dBm_AntSignal)
                if -100 <= rssi <= 0:
                    process_rssi(rssi)
        except Exception:
            pass

    while True:
        try:
            sniff(iface=iface, prn=handler, store=False,
                  count=0, monitor=True, timeout=5)
        except Exception as e:
            print(f"[!] Sniff error: {e} — retrying in 2s")
            time.sleep(2)

def simulate():
    print("[SIM] Running simulation — cycles: empty→stationary→moving")
    t = 0; phase = 'empty'; timer = 0
    while True:
        timer += 1
        if timer > int(PACKET_RATE * 8):
            timer = 0
            phase = {'empty':'stationary','stationary':'moving','moving':'empty'}[phase]
            print(f"[SIM] Phase → {phase}")
        base  = -55.0 + 2.0*np.sin(2*np.pi*t/(PACKET_RATE*10))
        noise = np.random.normal(0, 0.5)
        if phase == 'stationary':
            perturb = np.random.normal(0, 2.0)
        elif phase == 'moving':
            perturb = 4.0*np.sin(2*np.pi*0.8*t/PACKET_RATE) + np.random.normal(0,1.5)
        else:
            perturb = 0.0
        process_rssi(base + noise + perturb)
        t += 1
        time.sleep(1.0/PACKET_RATE)

def build_figure(sim_mode):
    fig = plt.figure(figsize=(14, 8), facecolor=BG)
    mode_str = "SIMULATION" if sim_mode else "LIVE"
    fig.canvas.manager.set_window_title(f'WiFi RSSI Detector [{mode_str}]')
    gs = gridspec.GridSpec(2, 2, figure=fig, hspace=0.45, wspace=0.30,
                           left=0.07, right=0.97, top=0.87, bottom=0.10)
    axes = {'rssi': fig.add_subplot(gs[0,:]),
            'zscore': fig.add_subplot(gs[1,0]),
            'fft': fig.add_subplot(gs[1,1])}
    for ax in axes.values():
        ax.set_facecolor(PANEL)
        ax.tick_params(colors=DIM, labelsize=8)
        for sp in ax.spines.values(): sp.set_color(BORDER)

    ax = axes['rssi']
    line_rssi, = ax.plot([], [], color=BLUE,   lw=1.0, label='RSSI (dBm)')
    line_mean, = ax.plot([], [], color=ORANGE, lw=1.0, ls='--', alpha=0.6, label='Rolling mean')
    ax.set_xlim(0, WINDOW); ax.set_ylim(-80, -30)
    ax.set_title('WiFi RSSI Signal (dBm)', color=WHITE, fontsize=10)
    ax.set_ylabel('RSSI (dBm)', color=DIM, fontsize=8)
    ax.legend(loc='upper right', fontsize=7, facecolor=PANEL, labelcolor=DIM)

    ax = axes['zscore']
    line_z,   = ax.plot([], [], color=RED,    lw=1.2, label='Z-score')
    line_var, = ax.plot([], [], color=ORANGE, lw=1.0, ls='--', label='Variance ratio')
    ax.axhline(y=Z_STATIONARY, color=ORANGE, ls=':', lw=0.8)
    ax.axhline(y=Z_MOVING,     color=RED,    ls=':', lw=0.8)
    ax.text(5, Z_STATIONARY+0.15, 'stationary', color=ORANGE, fontsize=7)
    ax.text(5, Z_MOVING+0.15,     'moving',     color=RED,    fontsize=7)
    ax.set_xlim(0, WINDOW); ax.set_ylim(0, 8)
    ax.set_title('Detection Scores', color=WHITE, fontsize=10)
    ax.set_ylabel('Score', color=DIM, fontsize=8)
    ax.legend(loc='upper right', fontsize=7, facecolor=PANEL, labelcolor=DIM)

    ax = axes['fft']
    line_fft, = ax.plot([], [], color=GREEN, lw=1.2)
    freq_line  = ax.axvline(x=0, color=ORANGE, ls='--', lw=0.8, alpha=0.7)
    ax.axvspan(0.1, 0.5, alpha=0.08, color=GREEN,  label='Breathing')
    ax.axvspan(0.5, 3.0, alpha=0.08, color=ORANGE, label='Movement')
    ax.set_xlim(0,4); ax.set_ylim(0,1.1)
    ax.set_title('FFT Motion Spectrum', color=WHITE, fontsize=10)
    ax.set_xlabel('Frequency (Hz)', color=DIM, fontsize=8)
    ax.set_ylabel('Magnitude (norm.)', color=DIM, fontsize=8)
    ax.legend(loc='upper right', fontsize=7, facecolor=PANEL, labelcolor=DIM)

    status_lbl = fig.text(0.5, 0.93, 'Initializing...', ha='center',
                          fontsize=15, color='yellow', fontweight='bold')
    info_lbl   = fig.text(0.02, 0.01, '', ha='left', fontsize=8, color=DIM)
    fig.suptitle(f'WiFi RSSI Human Presence Detector [{mode_str}]  |  MSEC / S.Shrikant & R.Mageshwar',
                 color=WHITE, fontsize=10, y=0.99)

    return fig, dict(line_rssi=line_rssi, line_mean=line_mean,
                     line_z=line_z, line_var=line_var,
                     line_fft=line_fft, freq_line=freq_line,
                     status_lbl=status_lbl, info_lbl=info_lbl, axes=axes)

def make_update(a):
    def update(frame):
        with lock:
            rssi = list(rssi_window); zsc = list(z_scores)
            vsc  = list(var_scores);  stat = status_text[0]
            pkts = packet_count[0];   dfrq = dominant_freq[0]
        a['line_rssi'].set_data(range(len(rssi)), rssi)
        if len(rssi) >= 10:
            roll = [np.mean(rssi[max(0,i-10):i+1]) for i in range(len(rssi))]
            a['line_mean'].set_data(range(len(roll)), roll)
        if rssi:
            a['axes']['rssi'].set_ylim(min(rssi)-3, max(rssi)+3)
        a['line_z'].set_data(range(len(zsc)), zsc)
        a['line_var'].set_data(range(len(vsc)), vsc)
        arr = np.array(rssi)
        if len(arr) >= FFT_WINDOW:
            seg   = arr[-FFT_WINDOW:] - np.mean(arr[-FFT_WINDOW:])
            mag   = np.abs(np.fft.rfft(seg * np.hanning(FFT_WINDOW)))
            frq   = np.fft.rfftfreq(FFT_WINDOW, d=1.0/PACKET_RATE)
            mask  = frq <= 4.0
            m_plt = mag[mask] / (mag[mask].max() + 1e-9)
            a['line_fft'].set_data(frq[mask], m_plt)
            a['freq_line'].set_xdata([dfrq])
        col = RED if 'MOVING' in stat else ORANGE if 'STATIONARY' in stat else DIM if 'Calibrating' in stat else GREEN
        a['status_lbl'].set_text(f'● {stat}')
        a['status_lbl'].set_color(col)
        a['info_lbl'].set_text(f'Packets: {pkts}   |   Dominant freq: {dfrq:.2f} Hz   |   Baseline: {len(baseline_buf)}/{BASELINE_LEN}')
        return (a['line_rssi'], a['line_mean'], a['line_z'], a['line_var'],
                a['line_fft'], a['freq_line'], a['status_lbl'], a['info_lbl'])
    return update

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--iface', default='wlp0s20f3')
    parser.add_argument('--sim',   action='store_true')
    args = parser.parse_args()

    signal.signal(signal.SIGINT,  signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    atexit.register(cleanup)

    if args.sim:
        threading.Thread(target=simulate, daemon=True).start()
    else:
        if os.geteuid() != 0:
            print("[!] Run with sudo"); sys.exit(1)
        iface_saved[0] = args.iface
        set_monitor_mode(args.iface)
        threading.Thread(target=sniff_packets, args=(args.iface,), daemon=True).start()

    fig, artists = build_figure(args.sim)
    animation.FuncAnimation(fig, make_update(artists),
                             interval=100, blit=False, cache_frame_data=False)
    plt.show()
    cleanup()

if __name__ == '__main__':
    main()
