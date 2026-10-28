import serial
import json
import time
import requests
import psutil
import os
import threading
import socket
from datetime import datetime, timezone

# ==========================================
# CONFIGURATION
# ==========================================
DEVICE_ID = "RICKY-001"
FIRMWARE_VERSION = "1.0.0"

BACKEND_BASE_URL = "http://ec2-65-0-97-54.ap-south-1.compute.amazonaws.com/api/v1" 

HEADERS = {
    "Authorization": "Bearer RICKY_PILOT_DEVICE_TOKEN_SECRET_2026",
    "Content-Type": "application/json"
}

UART_PORT = "/dev/ttyUSB0" 
BAUD_RATE = 9600 

TELEMETRY_INTERVAL = 3.0 

# ==========================================
# OFFLINE DATA CACHE (STORE & FORWARD)
# ==========================================
offline_cache = []
MAX_CACHE_SIZE = 1000  

# ==========================================
# PERSISTENT DEVICE STATE
# ==========================================
device_state = {
    "lat": 0.0, "lng": 0.0, "speed": 0.0, "gpsFix": False,
    "batPct": 0, "batVolts": 0.0, "charging": False,
    
    "sosActive": False, 
    "sosSource": None,
    "sos_cooldown_until": 0.0, 
    
    "imuAccelX": 0.0, "imuAccelY": 0.0, "imuAccelZ": 0.0,
    "imuGyroX": 0.0, "imuGyroY": 0.0, "imuGyroZ": 0.0
}

# ==========================================
# BACKGROUND THREADS
# ==========================================
def heartbeat_loop():
    url = f"{BACKEND_BASE_URL}/telemetry/heartbeat"
    while True:
        try: requests.post(url, json={"deviceId": DEVICE_ID, "timestamp": datetime.now(timezone.utc).isoformat()}, headers=HEADERS, timeout=5)
        except: pass
        time.sleep(30) 

def respond_to_command(cmd_id, status, response_text):
    url = f"{BACKEND_BASE_URL}/devices/{DEVICE_ID}/commands/{cmd_id}/respond"
    try: requests.post(url, json={"status": status, "response": response_text}, headers=HEADERS, timeout=5)
    except: pass

def command_polling_loop(ser):
    url = f"{BACKEND_BASE_URL}/devices/{DEVICE_ID}/commands/pending"
    while True:
        try:
            res = requests.get(url, headers=HEADERS, timeout=5)
            if res.status_code == 200:
                for cmd in res.json():
                    cmd_id, raw_cmd = cmd.get("id"), cmd.get("command", "")
                    cmd_text = str(raw_cmd).strip().upper() 
                    
                    if any(word in cmd_text for word in ["SOS", "CLEAR", "RESOLVE", "STOP", "DISABLE", "OFF"]):
                        print(f"\n[✅ RESOLVED] Dashboard successfully cleared the SOS Alert!")
                        
                        device_state["sosActive"] = False
                        device_state["sosSource"] = None
                        device_state["sos_cooldown_until"] = time.time() + 3.0 
                        
                        if ser and ser.is_open:
                            ser.reset_input_buffer()
                            ser.write(b"$SOSOFF#\n"); ser.flush()
                            time.sleep(0.1)
                            ser.write(b"$SOSOFF#\n"); ser.flush()
                            
                        respond_to_command(cmd_id, "executed", "SOS Alert forcefully resolved by Dashboard")
                        
                    elif cmd_text in ["REBOOT_DEVICE", "RESTART_PI"]:
                        respond_to_command(cmd_id, "executed", "Reboot sequence initiated")
                        time.sleep(1); os.system("sudo reboot")
        except: pass
        time.sleep(4) 

def keyboard_listener_loop(ser):
    while True:
        try:
            if input().strip() in ["$SOSOFF#", "SOSOFF"]:
                print("\n[KEYBOARD] Manual SOS Clear Triggered!")
                device_state["sosActive"] = False
                device_state["sosSource"] = None
                device_state["sos_cooldown_until"] = time.time() + 3.0 
                if ser and ser.is_open: 
                    ser.reset_input_buffer()
                    ser.write(b"$SOSOFF#\n"); ser.flush()
        except: time.sleep(1)

def get_system_metrics():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f: temp_c = int(f.read()) / 1000.0
    except: temp_c = 0.0
    return { "cpu": round(psutil.cpu_percent(interval=None)), "ram": round(psutil.virtual_memory().percent), "disk": round(psutil.disk_usage('/').percent), "temp": round(temp_c) }

def check_internet():
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=2.0)
        return True
    except OSError:
        pass
    return False

# ==========================================
# MAIN UART TELEMETRY LOOP
# ==========================================
def main():
    print(f"[STARTUP] Initializing Ricky Gateway for {DEVICE_ID} at {BAUD_RATE} baud...")
    psutil.cpu_percent(interval=None) 
    
    try: ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=1)
    except Exception as e: print(f"[ERROR] Failed to open UART: {e}"); return

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=command_polling_loop, args=(ser,), daemon=True).start()
    threading.Thread(target=keyboard_listener_loop, args=(ser,), daemon=True).start()

    telemetry_url = f"{BACKEND_BASE_URL}/telemetry"
    last_telemetry_time = 0.0

    while True:
        force_instant_telemetry = False  

        try:
            if ser.in_waiting > 0:
                raw_data = ser.readline().decode('utf-8', errors='ignore').strip()
                if not raw_data: continue
                
                safe_raw_data = raw_data.replace('$', '#$')
                messages = safe_raw_data.split('#')
                
                for msg in messages:
                    msg = msg.strip()
                    if not msg: continue
                        
                    if msg.startswith('$'):
                        clean = msg[1:] 
                        parts = [p.strip() for p in clean.split(',')]
                        
                        if clean.startswith("POWER"):
                            if len(parts) >= 4:
                                try:
                                    device_state["charging"] = (parts[1] == '1' or parts[1].lower() == 'true')
                                    device_state["batVolts"] = float(parts[2])
                                    device_state["batPct"] = int(float(parts[3]))
                                except Exception: pass
                                    
                        elif clean.startswith("GPS"):
                            if len(parts) >= 3:
                                try:
                                    lat_val, lng_val = float(parts[1]), float(parts[2])
                                    if lat_val != 0.0 and lng_val != 0.0:
                                        device_state["lat"], device_state["lng"] = lat_val, lng_val
                                        device_state["gpsFix"] = True
                                    else:
                                        device_state["gpsFix"] = False
                                        
                                    if len(parts) >= 4:
                                        device_state["gpsFix"] = (parts[3] == '1' or parts[3].lower() == 'true')
                                except Exception: pass
                                    
                        elif clean.startswith("IMU"):
                            if len(parts) >= 7:
                                try:
                                    device_state["imuAccelX"] = float(parts[1])
                                    device_state["imuAccelY"] = float(parts[2])
                                    device_state["imuAccelZ"] = float(parts[3])
                                    device_state["imuGyroX"] = float(parts[4])
                                    device_state["imuGyroY"] = float(parts[5])
                                    device_state["imuGyroZ"] = float(parts[6])
                                    device_state["speed"] = 0.0
                                except Exception: pass
                                    
                        elif clean.startswith("SOS,") or "SOSON" in clean:
                            if time.time() > device_state["sos_cooldown_until"]:
                                if not device_state["sosActive"]:
                                    print("\n" + "!"*55)
                                    print(" 🚨 [HARDWARE INTERRUPT] SOS BUTTON PRESSED!")
                                    print(" 🚨 Bypassing throttle to instantly alert backend...")
                                    print("!"*55 + "\n")
                                    force_instant_telemetry = True 
                                    
                                device_state["sosActive"] = True
                                
                                if clean.startswith("SOS,") and len(parts) >= 2:
                                    source_id = parts[1]
                                    if source_id in ['1', '0']: device_state["sosSource"] = "internal_button"
                                    elif source_id == '2': device_state["sosSource"] = "external_button_1"
                                    else: device_state["sosSource"] = "external_button"
                                else:
                                    device_state["sosSource"] = "internal_button"

                        elif "SOSOFF" in clean: 
                            pass

                    elif msg.startswith("{") and msg.endswith("}"):
                        try:
                            for key, value in json.loads(msg).items(): 
                                if key not in ["sosActive", "sosSource", "speed", "internet"]:
                                    device_state[key] = value
                        except Exception: pass

            else:
                time.sleep(0.01) 

            # ----------------------------------------------------
            # OFFLINE-READY TELEMETRY SENDER
            # ----------------------------------------------------
            current_time = time.time()
            if force_instant_telemetry or (current_time - last_telemetry_time >= TELEMETRY_INTERVAL):
                last_telemetry_time = current_time

                sys_health = get_system_metrics()
                is_charging = device_state["charging"]

                payload = {
                    "deviceId": DEVICE_ID, "firmwareVersion": FIRMWARE_VERSION,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "gps": { "lat": device_state["lat"], "lng": device_state["lng"], "speed": device_state["speed"], "fixed": device_state["gpsFix"] },
                    "battery": { "percentage": device_state["batPct"], "voltage": device_state["batVolts"], "charging": is_charging, "powerSource": "charging" if is_charging else "battery" },
                    "sos": { "active": device_state["sosActive"], "source": device_state["sosSource"] },
                    "imu": { "accelX": device_state["imuAccelX"], "accelY": device_state["imuAccelY"], "accelZ": device_state["imuAccelZ"], "gyroX": device_state["imuGyroX"], "gyroY": device_state["imuGyroY"], "gyroZ": device_state["imuGyroZ"] },
                    "hardware": { "espConnected": True, "gpsConnected": True, "imuConnected": True, "displayConnected": True },
                    "services": { "posterBookingRunning": True, "telemetryServiceRunning": True },
                    "system": { "cpu": sys_health["cpu"], "ram": sys_health["ram"], "disk": sys_health["disk"], "temp": sys_health["temp"] }
                }

                def send_network_payload(data_to_send, current_lat, current_lng, current_fix, current_bat, current_sos, current_source):
                    global offline_cache
                    
                    is_online = check_internet()
                    
                    # THE FIX: Internet is now sent as a pure Boolean (True/False) for Spring Boot!
                    data_to_send["internet"] = is_online
                    
                    fix_display = "FIXED" if current_fix else "SEARCHING"
                    sos_display = f"ACTIVE ({current_source})" if current_sos else "Safe"
                    
                    if is_online:
                        try:
                            res = requests.post(telemetry_url, json=data_to_send, headers=HEADERS, timeout=3)
                            
                            if res.status_code == 200:
                                print(f" -> [SENT] Net: ONLINE | Bat: {current_bat}% | GPS: {fix_display} ({current_lat}, {current_lng}) | SOS: {sos_display}")
                                
                                while offline_cache:
                                    cached_data = offline_cache[0]
                                    cached_data["internet"] = True # Keep cache as boolean too
                                    
                                    c_res = requests.post(telemetry_url, json=cached_data, headers=HEADERS, timeout=3)
                                    if c_res.status_code == 200:
                                        offline_cache.pop(0) 
                                    else:
                                        break 
                            else:
                                print(f" -> [BACKEND ERROR] Status {res.status_code}")
                                if len(offline_cache) < MAX_CACHE_SIZE: offline_cache.append(data_to_send)
                        except Exception:
                            if len(offline_cache) < MAX_CACHE_SIZE: offline_cache.append(data_to_send)
                    else:
                        if len(offline_cache) < MAX_CACHE_SIZE: offline_cache.append(data_to_send)
                        print(f" -> [OFFLINE] Net: DISCONNECTED | Bat: {current_bat}% | GPS: {fix_display} ({current_lat}, {current_lng}) | SOS: {sos_display}")

                threading.Thread(
                    target=send_network_payload, 
                    args=(payload.copy(), device_state["lat"], device_state["lng"], device_state["gpsFix"], device_state["batPct"], device_state["sosActive"], device_state["sosSource"])
                ).start()

        except Exception as e:
            time.sleep(1)

if __name__ == '__main__':
    main()
