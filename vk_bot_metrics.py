import os
import json
import time
import socket
import platform
import subprocess
from datetime import datetime
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

import psutil
import requests
import schedule
from threading import Thread

# Загружаем переменные из .env
load_dotenv()

# ========== КОНФИГ ИЗ ENV ==========
VK_TOKEN = os.getenv("VK_TOKEN")
VK_GROUP_ID = int(os.getenv("VK_GROUP_ID", 0))
CHAT_ID = int(os.getenv("CHAT_ID", 0))
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

SEND_INTERVAL_HOURS = int(os.getenv("SEND_INTERVAL_HOURS", 1))
METRICS_FILE = os.getenv("METRICS_CACHE_FILE", "/tmp/vk_metrics_cache.json")

ENABLE_SPEEDTEST = os.getenv("ENABLE_SPEEDTEST", "true").lower() == "true"
ENABLE_GPU_METRICS = os.getenv("ENABLE_GPU_METRICS", "true").lower() == "true"
ENABLE_SMART_DISK = os.getenv("ENABLE_SMART_DISK", "false").lower() == "true"

# Проверка наличия обязательных переменных
if not VK_TOKEN or VK_TOKEN == "ваш_токен_группы_здесь":
    print("⚠️ ОШИБКА: Не настроен VK_TOKEN в .env файле!")
    print("Создайте .env файл с вашими данными.")
    exit(1)

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def run_cmd(cmd: str, timeout: int = 10) -> str:
    """Выполняет команду и возвращает вывод"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except:
        return "N/A"

def get_public_ip() -> Dict[str, str]:
    """Получает публичный IP адрес и информацию о провайдере"""
    ip_info = {
        "ip": "N/A",
        "country": "N/A",
        "city": "N/A",
        "isp": "N/A"
    }
    
    # Пробуем несколько сервисов
    services = [
        "https://api.ipify.org?format=json",
        "https://ipapi.co/json/",
        "https://ipinfo.io/json"
    ]
    
    for service in services:
        try:
            response = requests.get(service, timeout=5)
            if response.status_code == 200:
                data = response.json()
                if "ip" in data:
                    ip_info["ip"] = data["ip"]
                if "country" in data:
                    ip_info["country"] = data.get("country", "N/A")
                if "city" in data:
                    ip_info["city"] = data.get("city", "N/A")
                if "isp" in data:
                    ip_info["isp"] = data.get("isp", "N/A")
                if "org" in data:
                    ip_info["isp"] = data.get("org", "N/A")
                break
        except:
            continue
    
    # Если не получилось через API, пробуем curl
    if ip_info["ip"] == "N/A":
        try:
            ip_info["ip"] = run_cmd("curl -s ifconfig.me || curl -s icanhazip.com")
        except:
            pass
    
    return ip_info

def get_network_speed() -> Dict[str, Any]:
    """Измеряет скорость интернета с помощью speedtest-cli"""
    speed_info = {
        "available": False,
        "download_mbps": 0,
        "upload_mbps": 0,
        "ping_ms": 0,
        "server": "N/A",
        "error": None
    }
    
    if not ENABLE_SPEEDTEST:
        speed_info["error"] = "Speedtest отключен в настройках"
        return speed_info
    
    try:
        # Проверяем, установлен ли speedtest-cli
        check = run_cmd("which speedtest-cli")
        if check == "N/A" or not check:
            speed_info["error"] = "speedtest-cli не установлен"
            return speed_info
        
        # Запускаем тест (простой режим, без лишнего вывода)
        result = run_cmd("speedtest-cli --simple", timeout=45)
        
        if result and result != "N/A" and result != "TIMEOUT":
            for line in result.split('\n'):
                line = line.strip()
                if 'Ping:' in line:
                    speed_info["ping_ms"] = float(line.split(':')[1].strip().replace('ms', ''))
                elif 'Download:' in line:
                    speed_info["download_mbps"] = float(line.split(':')[1].strip().replace('Mbit/s', ''))
                elif 'Upload:' in line:
                    speed_info["upload_mbps"] = float(line.split(':')[1].strip().replace('Mbit/s', ''))
            
            if speed_info["download_mbps"] > 0 or speed_info["upload_mbps"] > 0:
                speed_info["available"] = True
                speed_info["server"] = "Speedtest.net"
    except Exception as e:
        speed_info["error"] = str(e)
    
    return speed_info

def get_network_io() -> Dict[str, Any]:
    """Получает текущую скорость сетевых интерфейсов"""
    net_io = psutil.net_io_counters(pernic=True)
    interfaces = {}
    
    for iface, stats in net_io.items():
        if iface == 'lo':  # Пропускаем loopback
            continue
        interfaces[iface] = {
            "bytes_sent_mb": round(stats.bytes_sent / (1024**2), 2),
            "bytes_recv_mb": round(stats.bytes_recv / (1024**2), 2),
            "packets_sent": stats.packets_sent,
            "packets_recv": stats.packets_recv,
            "errin": stats.errin,
            "errout": stats.errout,
            "dropin": stats.dropin,
            "dropout": stats.dropout
        }
    
    # Общая статистика
    total = psutil.net_io_counters()
    
    return {
        "interfaces": interfaces,
        "total_sent_mb": round(total.bytes_sent / (1024**2), 2),
        "total_recv_mb": round(total.bytes_recv / (1024**2), 2),
        "total_packets": total.packets_sent + total.packets_recv
    }

def get_cpu_metrics() -> Dict[str, Any]:
    """Сбор метрик CPU"""
    cpu_percent = psutil.cpu_percent(interval=1, percpu=True)
    cpu_freq = psutil.cpu_freq()
    
    # Температуры через sensors
    temps = {}
    try:
        sensors_out = run_cmd("sensors -j")
        if sensors_out and sensors_out != "N/A" and sensors_out != "TIMEOUT":
            sensors_data = json.loads(sensors_out)
            for chip, data in sensors_data.items():
                if isinstance(data, dict):
                    for key, val in data.items():
                        if isinstance(val, dict) and 'temp1_input' in val:
                            temps[key] = float(val['temp1_input'])
                        elif isinstance(val, dict) and 'temp2_input' in val:
                            temps[key] = float(val['temp2_input'])
    except:
        pass
    
    # Загрузка через uptime
    load_avg = os.getloadavg() if hasattr(os, 'getloadavg') else (0, 0, 0)
    
    return {
        "percent_per_core": cpu_percent,
        "percent_avg": sum(cpu_percent) / len(cpu_percent) if cpu_percent else 0,
        "freq_mhz": cpu_freq.current if cpu_freq else 0,
        "freq_max": cpu_freq.max if cpu_freq else 0,
        "load_avg_1": load_avg[0],
        "load_avg_5": load_avg[1],
        "load_avg_15": load_avg[2],
        "temperatures": temps,
        "cores_count": psutil.cpu_count(),
        "cores_physical": psutil.cpu_count(logical=False)
    }

def get_gpu_metrics() -> Dict[str, Any]:
    """Сбор метрик NVIDIA GPU"""
    gpu_info = {
        "available": False,
        "count": 0,
        "gpus": []
    }
    
    if not ENABLE_GPU_METRICS:
        gpu_info["error"] = "GPU мониторинг отключен"
        return gpu_info
    
    try:
        nvidia_check = run_cmd("which nvidia-smi")
        if nvidia_check == "N/A" or not nvidia_check:
            return gpu_info
        
        nvidia_out = run_cmd("nvidia-smi --query-gpu=index,name,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free,power.draw,clocks.current.sm --format=csv,noheader,nounits", timeout=5)
        
        if nvidia_out and nvidia_out != "N/A" and nvidia_out != "TIMEOUT":
            for line in nvidia_out.split('\n'):
                if not line.strip():
                    continue
                parts = [x.strip() for x in line.split(',')]
                if len(parts) >= 7:
                    gpu_info["gpus"].append({
                        "index": int(parts[0]),
                        "name": parts[1],
                        "temp": float(parts[2]) if parts[2] else 0,
                        "util_percent": float(parts[3]) if parts[3] else 0,
                        "memory_total_mb": float(parts[4]) if parts[4] else 0,
                        "memory_used_mb": float(parts[5]) if parts[5] else 0,
                        "memory_free_mb": float(parts[6]) if parts[6] else 0,
                        "power_w": float(parts[7]) if len(parts) > 7 and parts[7] else 0,
                        "clock_mhz": float(parts[8]) if len(parts) > 8 and parts[8] else 0
                    })
            gpu_info["available"] = True
            gpu_info["count"] = len(gpu_info["gpus"])
    except:
        pass
    
    return gpu_info

def get_disk_metrics() -> Dict[str, Any]:
    """Сбор метрик дисков"""
    disks = []
    
    for partition in psutil.disk_partitions():
        if partition.fstype and 'loop' not in partition.device:
            try:
                usage = psutil.disk_usage(partition.mountpoint)
                disks.append({
                    "device": partition.device,
                    "mount": partition.mountpoint,
                    "fstype": partition.fstype,
                    "total_gb": round(usage.total / (1024**3), 2),
                    "used_gb": round(usage.used / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "used_percent": usage.percent
                })
            except:
                pass
    
    # SMART данные (опционально)
    smart_info = []
    if ENABLE_SMART_DISK:
        try:
            smart_out = run_cmd("sudo smartctl --scan 2>/dev/null")
            if smart_out and smart_out != "N/A" and smart_out != "TIMEOUT":
                for line in smart_out.split('\n'):
                    if line.strip():
                        dev = line.split()[0]
                        health = run_cmd(f"sudo smartctl -H {dev} 2>/dev/null | grep 'SMART overall-health'")
                        smart_info.append({
                            "device": dev,
                            "health": health.split(':')[-1].strip() if health else "Unknown"
                        })
        except:
            pass
    
    return {
        "partitions": disks,
        "smart_devices": smart_info,
        "total_disk_gb": sum(d["total_gb"] for d in disks),
        "used_disk_gb": sum(d["used_gb"] for d in disks),
        "free_disk_gb": sum(d["free_gb"] for d in disks)
    }

def get_system_info() -> Dict[str, Any]:
    """Общая информация о системе"""
    return {
        "hostname": socket.gethostname(),
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "os_full": run_cmd("lsb_release -ds 2>/dev/null") or platform.platform(),
        "kernel": platform.uname().release,
        "architecture": platform.machine(),
        "python_version": platform.python_version(),
        "uptime_seconds": time.time() - psutil.boot_time(),
        "uptime_formatted": format_uptime(time.time() - psutil.boot_time())
    }

def format_uptime(seconds: float) -> str:
    """Форматирует аптайм в человеческий вид"""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}д")
    if hours > 0:
        parts.append(f"{hours}ч")
    if minutes > 0:
        parts.append(f"{minutes}м")
    
    return " ".join(parts) if parts else "меньше минуты"

def collect_all_metrics() -> Dict[str, Any]:
    """Собирает ВСЕ метрики"""
    metrics = {
        "timestamp": datetime.now().isoformat(),
        "timestamp_unix": int(time.time()),
        "system": get_system_info(),
        "cpu": get_cpu_metrics(),
        "gpu": get_gpu_metrics(),
        "disk": get_disk_metrics(),
        "memory": {
            "total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
            "available_gb": round(psutil.virtual_memory().available / (1024**3), 2),
            "used_gb": round(psutil.virtual_memory().used / (1024**3), 2),
            "used_percent": psutil.virtual_memory().percent,
            "swap_total_gb": round(psutil.swap_memory().total / (1024**3), 2) if psutil.swap_memory() else 0,
            "swap_used_gb": round(psutil.swap_memory().used / (1024**3), 2) if psutil.swap_memory() else 0,
            "swap_percent": psutil.swap_memory().percent if psutil.swap_memory() else 0
        },
        "network": get_network_io(),
        "public_ip": get_public_ip(),
        "internet_speed": get_network_speed()
    }
    
    return metrics

def cache_metrics(metrics: Dict[str, Any]) -> None:
    """Сохраняет метрики в кэш"""
    try:
        with open(METRICS_FILE, 'w') as f:
            json.dump(metrics, f, indent=2)
    except Exception as e:
        print(f"⚠️ Ошибка сохранения кэша: {e}")

def get_cached_metrics() -> Dict[str, Any]:
    """Загружает метрики из кэша"""
    try:
        with open(METRICS_FILE, 'r') as f:
            return json.load(f)
    except:
        return collect_all_metrics()

# ========== ФОРМАТИРОВАНИЕ ==========

def format_metrics(metrics: Dict[str, Any], detailed: bool = True) -> str:
    """Форматирует метрики в красивый текст для отправки"""
    lines = []
    
    # Заголовок
    lines.append("🖥️ **МОНИТОРИНГ СЕРВЕРА**")
    lines.append(f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}")
    lines.append(f"⏱️ Аптайм: {metrics['system']['uptime_formatted']}")
    lines.append("")
    
    # Система и IP
    lines.append("📋 **СИСТЕМА**")
    lines.append(f"  • ОС: {metrics['system']['os_full']}")
    lines.append(f"  • Ядро: {metrics['system']['kernel']}")
    lines.append(f"  • Хост: {metrics['system']['hostname']}")
    lines.append(f"  • Архитектура: {metrics['system']['architecture']}")
    lines.append("")
    
    # Публичный IP
    ip = metrics['public_ip']
    lines.append("🌍 **IP ИНФОРМАЦИЯ**")
    lines.append(f"  • IP: `{ip['ip']}`")
    if ip['country'] != "N/A":
        lines.append(f"  • Страна: {ip['country']}")
    if ip['city'] != "N/A":
        lines.append(f"  • Город: {ip['city']}")
    if ip['isp'] != "N/A":
        lines.append(f"  • Провайдер: {ip['isp']}")
    lines.append("")
    
    # Скорость интернета
    speed = metrics['internet_speed']
    lines.append("📶 **СКОРОСТЬ ИНТЕРНЕТА**")
    if speed['available']:
        lines.append(f"  • Download: {speed['download_mbps']:.2f} Mbps")
        lines.append(f"  • Upload: {speed['upload_mbps']:.2f} Mbps")
        lines.append(f"  • Ping: {speed['ping_ms']:.1f} ms")
        lines.append(f"  • Сервер: {speed['server']}")
    else:
        error = speed.get('error', 'Недоступно')
        lines.append(f"  • ⚠️ {error}")
    lines.append("")
    
    # Сеть (трафик)
    net = metrics['network']
    lines.append("🌐 **СЕТЕВОЙ ТРАФИК**")
    lines.append(f"  • Отправлено всего: {net['total_sent_mb']:.1f} МБ")
    lines.append(f"  • Получено всего: {net['total_recv_mb']:.1f} МБ")
    lines.append(f"  • Всего пакетов: {net['total_packets']:,}")
    if net['interfaces']:
        for iface, stats in list(net['interfaces'].items())[:2]:
            lines.append(f"  • {iface}: {stats['bytes_sent_mb']:.1f}↑ / {stats['bytes_recv_mb']:.1f}↓ МБ")
    lines.append("")
    
    # CPU
    cpu = metrics['cpu']
    lines.append("⚡ **CPU**")
    lines.append(f"  • Ядер: {cpu['cores_count']} (физ: {cpu['cores_physical']})")
    lines.append(f"  • Загрузка: {cpu['percent_avg']:.1f}%")
    lines.append(f"  • Частота: {cpu['freq_mhz']:.0f} МГц")
    lines.append(f"  • LA (1/5/15): {cpu['load_avg_1']:.2f} / {cpu['load_avg_5']:.2f} / {cpu['load_avg_15']:.2f}")
    
    if cpu['temperatures']:
        temp_lines = []
        for name, temp in list(cpu['temperatures'].items())[:3]:
            # Очищаем имя для красоты
            clean_name = name.replace('_input', '').replace('temp1', 'CPU').replace('temp2', 'CPU2')
            temp_lines.append(f"{clean_name}: {temp:.1f}°C")
        lines.append(f"  • Темп: {' | '.join(temp_lines)}")
    else:
        lines.append("  • Темп: N/A")
    lines.append("")
    
    # GPU
    gpu = metrics['gpu']
    if gpu.get('available', False) and gpu.get('gpus'):
        lines.append("🎮 **GPU**")
        for g in gpu['gpus']:
            lines.append(f"  • GPU {g['index']}: {g['name']}")
            lines.append(f"    - Загрузка: {g['util_percent']:.1f}%")
            lines.append(f"    - Темп: {g['temp']:.1f}°C")
            lines.append(f"    - Память: {g['memory_used_mb']:.0f}/{g['memory_total_mb']:.0f} МБ ({g['memory_used_mb']/g['memory_total_mb']*100:.1f}%)")
            if g.get('power_w', 0) > 0:
                lines.append(f"    - Потребление: {g['power_w']:.1f}W")
        lines.append("")
    
    # Память
    mem = metrics['memory']
    lines.append("💾 **ОПЕРАТИВНАЯ ПАМЯТЬ**")
    lines.append(f"  • RAM: {mem['used_gb']:.1f}/{mem['total_gb']:.1f} ГБ ({mem['used_percent']:.1f}%)")
    if mem['swap_total_gb'] > 0:
        lines.append(f"  • Swap: {mem['swap_used_gb']:.1f}/{mem['swap_total_gb']:.1f} ГБ ({mem['swap_percent']:.1f}%)")
    lines.append("")
    
    # Диски
    disk = metrics['disk']
    lines.append("💿 **ДИСКИ**")
    for d in disk['partitions'][:3]:  # Показываем первые 3 раздела
        bar = "█" * int(d['used_percent'] / 5) + "░" * (20 - int(d['used_percent'] / 5))
        lines.append(f"  • {d['device']} ({d['mount']}):")
        lines.append(f"    {bar} {d['used_percent']:.1f}%")
        lines.append(f"    {d['used_gb']:.1f}/{d['total_gb']:.1f} ГБ")
    if len(disk['partitions']) > 3:
        lines.append(f"  • ... и еще {len(disk['partitions']) - 3} разделов")
    lines.append(f"  • Всего: {disk['total_disk_gb']:.1f} ГБ")
    lines.append(f"  • Занято: {disk['used_disk_gb']:.1f} ГБ ({disk['used_disk_gb']/disk['total_disk_gb']*100 if disk['total_disk_gb'] > 0 else 0:.1f}%)")
    
    if disk['smart_devices'] and ENABLE_SMART_DISK:
        lines.append("  • SMART:")
        for smart in disk['smart_devices']:
            lines.append(f"    - {smart['device']}: {smart['health']}")
    lines.append("")
    
    if detailed:
        lines.append("---")
        lines.append(f"🔄 Обновлено: {datetime.fromtimestamp(metrics['timestamp_unix']).strftime('%H:%M:%S')}")
        lines.append("❓ /help - все команды")
    
    return "\n".join(lines)

# ========== ОТПРАВКА В ВК ==========

def send_vk_message(text: str, peer_id: int = None) -> bool:
    """Отправляет сообщение в ВК"""
    if not VK_TOKEN:
        print("⚠️ Нет токена ВК!")
        return False
    
    if peer_id is None:
        peer_id = CHAT_ID
    
    url = "https://api.vk.com/method/messages.send"
    params = {
        "access_token": VK_TOKEN,
        "v": "5.131",
        "peer_id": peer_id,
        "message": text,
        "random_id": int(time.time() * 1000)
    }
    
    try:
        response = requests.post(url, params=params, timeout=10)
        data = response.json()
        
        if "error" in data:
            print(f"❌ Ошибка ВК: {data['error']['error_msg']}")
            return False
        
        print(f"✅ Сообщение отправлено в {peer_id}")
        return True
    except Exception as e:
        print(f"❌ Ошибка отправки: {e}")
        return False

def send_metrics_report() -> bool:
    """Отправляет свежий отчет с метриками"""
    print("📊 Сбор свежих метрик...")
    metrics = collect_all_metrics()
    cache_metrics(metrics)
    text = format_metrics(metrics, detailed=True)
    return send_vk_message(text)

def send_cached_report() -> bool:
    """Отправляет кэшированный отчет"""
    metrics = get_cached_metrics()
    text = format_metrics(metrics, detailed=True)
    return send_vk_message(text)

# ========== ОБРАБОТКА КОМАНД ВК ==========

def handle_vk_commands():
    """Обрабатывает входящие сообщения через LongPoll API"""
    last_ts = 0
    url = "https://api.vk.com/method/messages.get"
    
    print("👂 Начинаем прослушивание команд...")
    
    while True:
        try:
            params = {
                "access_token": VK_TOKEN,
                "v": "5.131",
                "count": 5,
                "out": 0
            }
            
            response = requests.get(url, params=params, timeout=10)
            data = response.json()
            
            if "response" in data and "items" in data["response"]:
                for msg in data["response"]["items"]:
                    if msg["date"] <= last_ts:
                        continue
                    
                    last_ts = msg["date"]
                    text = msg.get("text", "").lower().strip()
                    peer_id = msg["peer_id"]
                    from_id = msg["from_id"]
                    
                    # Обработка команд
                    if text in ["/start", "старт", "привет"]:
                        send_vk_message(
                            "👋 Привет! Я бот-мониторинг сервера.\n\n"
                            "📊 **Доступные команды:**\n"
                            "/metrics - свежие метрики (сбор ~5-10 сек)\n"
                            "/cached - быстрые метрики (из кэша)\n"
                            "/speed - тест скорости интернета\n"
                            "/ip - показать IP адрес\n"
                            "/help - полная справка",
                            peer_id
                        )
                    
                    elif text in ["/metrics", "метрики"]:
                        send_metrics_report()
                    
                    elif text in ["/cached", "кэш"]:
                        send_cached_report()
                    
                    elif text in ["/speed", "скорость"]:
                        send_vk_message("📶 Измеряю скорость интернета... Это займет ~30 секунд ⏳", peer_id)
                        metrics = collect_all_metrics()
                        cache_metrics(metrics)
                        speed = metrics['internet_speed']
                        if speed['available']:
                            msg = f"📶 **Скорость интернета:**\n"
                            msg += f"⬇️ Download: {speed['download_mbps']:.2f} Mbps\n"
                            msg += f"⬆️ Upload: {speed['upload_mbps']:.2f} Mbps\n"
                            msg += f"📡 Ping: {speed['ping_ms']:.1f} ms\n"
                            msg += f"🌐 Сервер: {speed['server']}"
                        else:
                            msg = f"❌ Не удалось измерить скорость: {speed.get('error', 'неизвестная ошибка')}"
                        send_vk_message(msg, peer_id)
                    
                    elif text in ["/ip", "айпи"]:
                        ip = get_public_ip()
                        msg = f"🌍 **IP информация:**\n"
                        msg += f"IP: `{ip['ip']}`\n"
                        if ip['country'] != "N/A":
                            msg += f"Страна: {ip['country']}\n"
                        if ip['city'] != "N/A":
                            msg += f"Город: {ip['city']}\n"
                        if ip['isp'] != "N/A":
                            msg += f"Провайдер: {ip['isp']}"
                        send_vk_message(msg, peer_id)
                    
                    elif text == "/help" or text == "помощь":
                        help_text = """🤖 **Доступные команды:**

**Основные:**
/start - Приветствие
/metrics - Свежие метрики (сбор 5-10 сек)
/cached - Быстрые метрики (из кэша)

**Специальные:**
/speed - Тест скорости интернета
/ip - Показать IP адрес
/help - Эта справка

**Автоматика:**
Отчеты отправляются каждый час автоматически.

**Метрики:**
• CPU (загрузка, частота, температура)
• GPU (если есть)
• Память (RAM + Swap)
• Диски (разделы, занятость)
• Сеть (трафик)
• Интернет (скорость, ping)
• IP (адрес, провайдер)
• Система (ОС, ядро, аптайм)"""
                        send_vk_message(help_text, peer_id)
                    
                    elif text == "ping":
                        send_vk_message("🏓 Pong! Бот жив и работает.", peer_id)
                    
            time.sleep(2)
            
        except Exception as e:
            print(f"❌ Ошибка в polling: {e}")
            time.sleep(10)

# ========== ЗАПУСК ==========

def run_scheduler():
    """Запускает планировщик отправок"""
    schedule.every(SEND_INTERVAL_HOURS).hours.do(send_metrics_report)
    
    print(f"⏰ Автоотправка каждые {SEND_INTERVAL_HOURS} час(а)")
    
    while True:
        schedule.run_pending()
        time.sleep(60)

def main():
    """Основная функция"""
    print("=" * 50)
    print("🚀 ЗАПУСК VK БОТА МОНИТОРИНГА")
    print("=" * 50)
    
    print(f"📁 Конфиг: .env")
    print(f"📁 Кэш: {METRICS_FILE}")
    print(f"🔄 Интервал: {SEND_INTERVAL_HOURS} час(а)")
    print(f"📶 Speedtest: {'ВКЛ' if ENABLE_SPEEDTEST else 'ВЫКЛ'}")
    print(f"🎮 GPU: {'ВКЛ' if ENABLE_GPU_METRICS else 'ВЫКЛ'}")
    print("=" * 50)
    
    # Первичный сбор и кэширование
    print("📊 Первичный сбор метрик...")
    metrics = collect_all_metrics()
    cache_metrics(metrics)
    print("✅ Метрики сохранены в кэш")
    
    # Запускаем обработчик команд в отдельном потоке
    command_thread = Thread(target=handle_vk_commands, daemon=True)
    command_thread.start()
    
    # Запускаем планировщик в основном потоке
    try:
        run_scheduler()
    except KeyboardInterrupt:
        print("\n⏹️ Бот остановлен пользователем")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")

if __name__ == "__main__":
    main()
