# -*- coding: utf-8 -*-
"""
DHO KEMALREİS - YER KONTROL (PySide6)
Sağ tarafa harita paneli eklendi:
 - '12.jpg' ortalanır ve 0.5x zoom ile açılır
 - Zoom In/Out butonları
 - Fare ile sürükleyerek (drag) gezinme
 - MEVCUT_KONUM (lat/lon) canlı işaretlenir ve iz bırakır
 - GÖREV_NOKTALARI (GPS1..5) gelirse haritada etiketlenir

Gereklilikler:
  pip install PySide6 pyserial
(Projeye internet gerekmez. pyproj yok; WebMercator dönüşümü için formüller içeride.)
"""
from __future__ import annotations
import sys, os, json, csv, time, math
from typing import Optional, Dict, Any

from PySide6 import QtCore, QtWidgets, QtGui
from PySide6.QtCore import Qt, Signal, QThread

import serial
import serial.tools.list_ports
from PySide6.QtGui import QImageReader
from collections import OrderedDict

import cv2
import numpy as np
import socket
import struct

def res_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))  # onefile için _MEIPASS
    return os.path.join(base, name)

# ---- Harita resmi ve köşe koordinatları (WGS84) ----
MAP_IMAGE_PATH = res_path("roboboat.jpg")
# Sol-Üst (NW) ve Sağ-Alt (SE) köşeler (WGS84 derece)
NORTH = 27.381
SOUTH = 27.356
WEST  = -82.455
EAST  = -82.445
# ---- Marker boyutları ----
WP_RADIUS_PX = 1.2        # Waypoint dairesi yarıçapı (px)
WP_FONT_PT   = 1.4       # Waypoint etiket yazı boyutu (pt)
CURR_RADIUS_PX = 1.0      # Mevcut konum noktası yarıçapı (px)
PATH_WIDTH_PX  = 1.0      # İz çizgisi kalınlığı (px)
KEEP_PIXEL_SIZE = False   # Zoom yapsan da ikonlar ekranda aynı piksel boyutunda kalsın mı?

KIRMIZI_CIZGI_KALINLIK = 0.7
GPS_UPDATE_INTERVAL_MS = 5000  # Yalnızca GPS1..GPS5 5 sn'de bir uygulanır

# --- Halat kenar şeridi (döşeme çizer) ---
class RopeStripe(QtWidgets.QWidget):
    def __init__(self, side: str, pix_path: str, thickness: int, parent=None):
        """
        side: "top" | "bottom" | "left" | "right"
        pix_path: halat.png yolu (2560x51)
        thickness: şerit kalınlığı (px)
        """
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)  # tıklamaları engellemesin
        self._side = side
        self._thk = int(thickness)

        pm = QtGui.QPixmap(pix_path)
        if pm.isNull():
            # fallback: gri
            pm = QtGui.QPixmap(512, 51)
            pm.fill(QtGui.QColor("#777"))
        # Dikey kenarlar için 90° döndür
        if side in ("left", "right"):
            pm = pm.transformed(QtGui.QTransform().rotate(90), QtCore.Qt.SmoothTransformation)
        self._pm = pm

        # Boyutu sabitle (geometriyi yine de dışarıdan set edeceğiz)
        if side in ("top", "bottom"):
            self.setFixedHeight(self._thk)
        else:
            self.setFixedWidth(self._thk)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        # Tüm rect’i halat dokusuyla döşe
        p.drawTiledPixmap(self.rect(), self._pm)

class HugeImageItem(QtWidgets.QGraphicsItem):
    """
    Tam çözünürlükte dev JPEG/PNG'leri karo (tile) halinde tembel yükleyip çizer.
    QImageReader.setClipRect ile sadece gereken bölgeyi diskten decode eder.
    Basit LRU cache ile ekranda görünen karolar tutulur.
    """
    def __init__(self, image_path: str, tile_size: int = 2048, cache_limit: int = 128, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.tile_size = int(tile_size)
        self.cache_limit = int(cache_limit)  # aynı anda tutulacak en fazla karo sayısı
        self._cache: OrderedDict[tuple[int,int], QtGui.QPixmap] = OrderedDict()

        r = QImageReader(self.image_path)
        r.setAutoTransform(True)
        sz = r.size()
        if not sz.isValid():
            # Boyut metadata’sı yoksa bir kere okuyup boyut al
            img = r.read()
            if img.isNull():
                raise RuntimeError(f"Image cannot be read: {self.image_path}")
            self._W, self._H = img.width(), img.height()
        else:
            self._W, self._H = sz.width(), sz.height()

        self.setFlag(QtWidgets.QGraphicsItem.ItemUsesExtendedStyleOption, True)

    # genel boyut
    @property
    def width(self):  return self._W
    @property
    def height(self): return self._H

    def boundingRect(self) -> QtCore.QRectF:
        return QtCore.QRectF(0, 0, self._W, self._H)

    def _load_tile(self, tx: int, ty: int) -> QtGui.QPixmap:
        key = (tx, ty)
        if key in self._cache:
            # LRU güncelle
            pm = self._cache.pop(key)
            self._cache[key] = pm
            return pm

        x = tx * self.tile_size
        y = ty * self.tile_size
        w = min(self.tile_size, self._W - x)
        h = min(self.tile_size, self._H - y)

        r = QImageReader(self.image_path)
        r.setAutoTransform(True)
        r.setClipRect(QtCore.QRect(x, y, w, h))
        img = r.read()
        if img.isNull():
            # başarısızsa gri placeholder
            pm = QtGui.QPixmap(w, h)
            pm.fill(QtGui.QColor("#777"))
        else:
            pm = QtGui.QPixmap.fromImage(img)

        # LRU: kapasiteyi koru
        self._cache[key] = pm
        if len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)  # en eskiyi at
        return pm

    def paint(self, painter: QtGui.QPainter, option: QtWidgets.QStyleOptionGraphicsItem, widget=None):
        # Görünen dikdörtgen (exposed rect) → hangi karolar gerekli?
        rect = option.exposedRect.intersected(self.boundingRect())
        if rect.isEmpty():
            return

        ts = self.tile_size
        x0 = int(rect.left() // ts)
        y0 = int(rect.top()  // ts)
        x1 = int((rect.right()) // ts)
        y1 = int((rect.bottom()) // ts)

        # bir miktar tampon karoyu da getir (daha akıcı pan/zoom)
        pad = 1
        x0 = max(0, x0 - pad)
        y0 = max(0, y0 - pad)
        x1 = min((self._W - 1) // ts, x1 + pad)
        y1 = min((self._H - 1) // ts, y1 + pad)

        for ty in range(y0, y1 + 1):
            for tx in range(x0, x1 + 1):
                pm = self._load_tile(tx, ty)
                painter.drawPixmap(tx * ts, ty * ts, pm)

def normalize_gps_dict(gps_raw):
    """
    Kabul edilen örnek biçimler:
      {"GPS1":{"lat":..,"lon":..}, ...}
      {"GPS1_enlem":..,"GPS1_boylam":.., ...}
      {"GPS1":[lat,lon], ...}  veya {"GPS1":"lat,lon"}
      {"points":[{"name":"GPS1","lat":..,"lon":..}, ...]}
    Dönüş: {"GPS1":{"lat":float,"lon":float}, ...}
    """
    if not isinstance(gps_raw, dict):
        return None

    out = {}
    # 1) Zaten nested dict ise
    ok_nested = True
    for i in range(1, 6):
        k = f"GPS{i}"
        v = gps_raw.get(k)
        if not (isinstance(v, dict) and "lat" in v and "lon" in v):
            ok_nested = False
            break
    if ok_nested:
        # hepsini float’a çevir
        for i in range(1, 6):
            k = f"GPS{i}"
            v = gps_raw.get(k)
            if v is None:
                continue
            try:
                out[k] = {"lat": float(v["lat"]), "lon": float(v["lon"])}
            except Exception:
                pass
        return out if out else None

    # 2) GPS1_enlem / GPS1_boylam
    any_found = False
    for i in range(1, 6):
        ke = f"GPS{i}_enlem"
        kb = f"GPS{i}_boylam"
        if ke in gps_raw and kb in gps_raw:
            try:
                out[f"GPS{i}"] = {"lat": float(gps_raw[ke]), "lon": float(gps_raw[kb])}
                any_found = True
            except Exception:
                pass
    if any_found:
        return out

    # 3) GPS1: [lat,lon]  ya da "lat,lon"
    any_found = False
    for i in range(1, 6):
        k = f"GPS{i}"
        if k in gps_raw:
            v = gps_raw[k]
            try:
                if isinstance(v, (list, tuple)) and len(v) == 2:
                    lat, lon = float(v[0]), float(v[1])
                elif isinstance(v, str) and "," in v:
                    a, b = v.split(",", 1)
                    lat, lon = float(a), float(b)
                else:
                    continue
                out[k] = {"lat": lat, "lon": lon}
                any_found = True
            except Exception:
                pass
    if any_found:
        return out

    # 4) {"points":[{"name":"GPS1","lat":..,"lon":..}, ...]}
    pts = gps_raw.get("points")
    if isinstance(pts, list):
        for p in pts:
            try:
                name = str(p.get("name"))
                if name.upper().startswith("GPS"):
                    out[name.upper()] = {"lat": float(p["lat"]), "lon": float(p["lon"])}
            except Exception:
                pass
        if out:
            return out

    return None

# -------------------- Serial Worker --------------------
class SerialWorker(QThread):
    packet = Signal(dict)
    status = Signal(str)
    link = Signal(bool)

    def __init__(self, port: str, baud: int, csv_path: str | None = None):
        super().__init__()
        self.port = port
        self.baud = baud
        self.csv_path = csv_path
        self._stop = False
        self.ser: Optional[serial.Serial] = None
        self._tx_queue: list[str] = []
        self._csv_file = None
        self._csv_writer = None

    def configure(self, port: str, baud: int, csv_path: str | None):
        self.port = port
        self.baud = baud
        self.csv_path = csv_path

    def queue_send(self, obj: Dict[str, Any]):
        try:
            line = json.dumps(obj, ensure_ascii=False) + "\r\n"
            self._tx_queue.append(line)
        except Exception as e:
            self.status.emit(f"Serialize error: {e}")

    def run(self):
        # CSV open (optional)
        if self.csv_path:
            try:
                self._csv_file = open(self.csv_path, "a", newline="", encoding="utf-8")
                self._csv_writer = csv.writer(self._csv_file)
                if self._csv_file.tell() == 0:
                    self._csv_writer.writerow([
                        "t_ms", "FPS", "SOL_PWM", "SAG_PWM", "HIZ_mps", "HDG_deg", "HDG_HEDEF_deg",
                        "HDG_SAGLIGI", "SONRAKI_NOKTA", "LAT", "LON", "KALAN_m", "MANUEL"
                    ])
            except Exception as e:
                self.status.emit(f"CSV open failed: {e}")
                self._csv_file = None
                self._csv_writer = None

        while not self._stop:
            # Ensure serial is open
            if self.ser is None or not self.ser.is_open:
                self._open_serial()
                if self._stop:
                    break

            # Transmit queued messages
            try:
                while self._tx_queue:
                    line = self._tx_queue.pop(0)
                    self.ser.write(line.encode("utf-8"))
            except Exception as e:
                self.status.emit(f"TX error: {e}")

            # Read one line
            try:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
                if line:
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            self.packet.emit(obj)
                            # CSV log
                            if self._csv_writer:
                                lat = None
                                lon = None
                                pos = obj.get("MEVCUT_KONUM")
                                if isinstance(pos, dict):
                                    lat = pos.get("lat")
                                    lon = pos.get("lon")
                                self._csv_writer.writerow([
                                    obj.get("t_ms"),
                                    obj.get("FPS", obj.get("fps")),
                                    obj.get("SOL_İTİCİ_İTKİ_İSTEĞİ_PWM"),
                                    obj.get("SAĞ_İTİCİ_İTKİ_İSTEĞİ_PWM"),
                                    obj.get("İDA_GERÇEK_HIZ_mps"),
                                    obj.get("GERÇEK_HEADING_deg"),
                                    obj.get("HEDEF_HEADING_deg"),
                                    obj.get("HEADING_SAĞLIĞI"),
                                    obj.get("SONRAKİ_GÖREV_NOKTASI"),
                                    lat, lon,
                                    obj.get("KALAN_MESAFE_m"),
                                    obj.get("MANUEL_MOD"),
                                ])
                                self._csv_file.flush()
                    except Exception:
                        # try to salvage merged jsons
                        if "}{" in line:
                            parts = line.split("}{")
                            for i, part in enumerate(parts):
                                s = part
                                if i > 0:
                                    s = "{" + s
                                if i < len(parts) - 1:
                                    s = s + "}"
                                try:
                                    obj = json.loads(s)
                                    if isinstance(obj, dict):
                                        self.packet.emit(obj)
                                except Exception:
                                    pass
            except (serial.SerialException, OSError) as e:
                self.status.emit(f"Disconnected: {e}")
                self.link.emit(False)
                self._close_serial()
                time.sleep(1.0)
            except Exception as e:
                self.status.emit(f"RX error: {e}")

        self._close_serial()
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass

    def stop(self):
        self._stop = True

    def _open_serial(self):
        # Wait until target port appears
        avail = [p.device for p in serial.tools.list_ports.comports()]
        if self.port not in avail:
            self.status.emit(f"Waiting for {self.port} (available: {avail})...")
        while not self._stop:
            try:
                self.ser = serial.Serial(self.port, self.baud, timeout=0.2)
                self.status.emit(f"Connected {self.port} @ {self.baud}")
                self.link.emit(True)
                return
            except Exception:
                time.sleep(0.7)

    def _close_serial(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

# -------------------- Video Worker(kamera görüntüsü almak için) ------------------
class VideoWorker(QThread):
    frame_signal = Signal(QtGui.QImage)

    def __init__(self, port=5000):
        super().__init__()
        self.port = port
        self._running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Soket buffer boyutunu artırıyoruz ki yüksek çözünürlükte taşma olmasın
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024 * 5)

        # Tüm IP'lerden dinle (0.0.0.0)
        try:
            self.sock.bind(("0.0.0.0", self.port))
        except Exception as e:
            print(f"Video soket hatası: {e}")

    def run(self):
        # Basit bir UDP frame alıcısı (Datagram boyutu sınırına dikkat edilmeli,
        # buradaki mantık 60k altı paketler veya basit JPEG chunkları içindir.
        # Daha profesyonel akış için GStreamer kullanılır ama bu Python için en pratik yoldur.)

        while self._running:
            try:
                # Maksimum UDP paket boyutu (65507 byte).
                # İDA tarafında görüntüyü sıkıştırıp (JPEG quality) tek pakete sığdırmak
                # veya parçalayıp göndermek gerekebilir. Bu örnek tek paket mantığıdır.
                data, _ = self.sock.recvfrom(65535)

                if len(data) > 0:
                    # Gelen byte verisini numpy array'e çevir
                    np_arr = np.frombuffer(data, dtype=np.uint8)
                    # Resmi decode et
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

                    if frame is not None:
                        # BGR'den RGB'ye çevir (Qt RGB sever)
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        h, w, ch = frame.shape
                        bytes_per_line = ch * w

                        # QImage oluştur
                        qt_img = QtGui.QImage(frame.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)

                        # Sinyal ile gönder (copy() önemli, yoksa buffer silinir)
                        self.frame_signal.emit(qt_img.copy())
            except Exception:
                pass

    def stop(self):
        self._running = False
        self.sock.close()

#-------------fare ile zoom---------------
class  GraphicsView(QtWidgets.QGraphicsView):
    zoomChanged = QtCore.Signal(float)   # EKLENDİ
    def __init__(self, scene, parent=None, min_zoom=0.25, max_zoom=16.0):
        super().__init__(scene, parent)
        self._min_zoom = float(min_zoom)
        self._max_zoom = float(max_zoom)
        # Zoom imlecin altında odaklansın:
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QtWidgets.QGraphicsView.AnchorViewCenter)
        # Sürükleyerek pan
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)

        # Daha pürüzsüz görüntü
        self.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)

    def wheelEvent(self, event: QtGui.QWheelEvent):
        delta = event.pixelDelta().y() if not event.angleDelta().y() else event.angleDelta().y()
        if delta == 0:
            event.ignore(); return
        step_factor = 1.15 if delta > 0 else (1.0/1.15)
        cur = self.transform().m11()
        new_zoom = max(self._min_zoom, min(self._max_zoom, cur * step_factor))
        if new_zoom == cur:
            return
        factor = new_zoom / cur
        self.scale(factor, factor)
        smooth = new_zoom >= 1.0
        self.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, smooth)
        self.zoomChanged.emit(new_zoom)   # EKLENDİ
        event.accept()

    clicked = Signal(QtCore.QPointF, QtCore.QPoint)  # scenePos, globalPos
    moved = Signal(QtCore.QPointF)  # scenePos

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == Qt.LeftButton:
            view_pt = event.position().toPoint()  # pos() yerine
            self.clicked.emit(
                self.mapToScene(view_pt),
                event.globalPosition().toPoint()  # mapToGlobal(pos()) yerine
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        self.moved.emit(self.mapToScene(event.position().toPoint()))  # pos() yerine
        super().mouseMoveEvent(event)


# -------------------- Map Panel --------------------
class MapPanel(QtWidgets.QWidget):
    """QGraphicsView tabanlı basit görüntü haritası.
    - Drag: sol tuş ile sürükle
    - Zoom: butonlar +/-, ayrıca tekerlek destekli
    - Lat/Lon -> piksel dönüşümü WebMercator formülleri ile
    """
    def __init__(self, image_path: str, north: float, south: float, west: float, east: float, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(420)

        self.image_path = image_path
        self.north, self.south, self.west, self.east = north, south, west, east
        self._R = 6378137.0  # WebMercator yarıçap
        self._max_zoom = 16.0
        self._min_zoom = 0.25

        # Scene & View
        self.scene = QtWidgets.QGraphicsScene(self)
        self.view = GraphicsView(self.scene, self, min_zoom=self._min_zoom, max_zoom=self._max_zoom)
        self.view.setRenderHint(QtGui.QPainter.Antialiasing, True)
        self.view.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
        self.view.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.view.setViewportUpdateMode(QtWidgets.QGraphicsView.FullViewportUpdate)

        # Pixmap (büyük görselleri otomatik küçült)
        MAX_DIM = 32000  # QPixmap/texture güvenli sınır

        # --- TAM ÇÖZÜNÜRLÜKTE KARO TABANLI YÜKLEME ---
        if not os.path.exists(self.image_path):
            pm = QtGui.QPixmap(800, 600)
            pm.fill(QtGui.QColor("#555"))
            painter = QtGui.QPainter(pm)
            painter.setPen(QtGui.QPen(Qt.white))
            painter.drawText(pm.rect(), Qt.AlignCenter, f"Bulunamadı:\n{self.image_path}")
            painter.end()
            # yine de sahne boyutu olsun
            self._img_W, self._img_H = pm.width(), pm.height()
            self.image_item = self.scene.addPixmap(pm)
        else:
            # Tam çözünürlükte, yalnızca görünen alanı yükleyen karo öğesi
            self.image_item = HugeImageItem(self.image_path, tile_size=2048, cache_limit=128)
            self.scene.addItem(self.image_item)
            self._img_W, self._img_H = self.image_item.width, self.image_item.height


        # Overlay grubu (işaretler vs.)
        self.overlay_group = QtWidgets.QGraphicsItemGroup()
        self.overlay_group.setZValue(10)
        self.scene.addItem(self.overlay_group)

        # Mevcut konum işaretçisi (kırmızı)
        r = CURR_RADIUS_PX
        self.curr_marker = QtWidgets.QGraphicsEllipseItem(-r, -r, 2 * r, 2 * r)
        self.curr_marker.setBrush(QtGui.QBrush(QtGui.QColor("#ff1744")))
        self.curr_marker.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.0))
        self.curr_marker.setZValue(20)
        if KEEP_PIXEL_SIZE:
            from PySide6.QtWidgets import QGraphicsItem
            self.curr_marker.setFlag(QGraphicsItem.ItemIgnoresTransformations, True)
        self.curr_marker.setVisible(False)
        self.scene.addItem(self.curr_marker)

        # >>> İZ (polyline) – ÖNCE KALEMİ OLUŞTUR, SONRA GENİŞLİK VER <<<
        self.path_pen = QtGui.QPen(QtGui.QColor("#ff5252"))
        self.path_pen.setWidthF(PATH_WIDTH_PX)
        self.path_path = QtGui.QPainterPath()
        self.path_item = self.scene.addPath(self.path_path, self.path_pen)
        self.path_item.setZValue(12)

        # --- Heading overlays (real & target) ---
        self._last_lat = None
        self._last_lon = None
        self._real_hdg_deg = None  # degrees, 0 = True North, CW positive
        self._targ_hdg_deg = None

        # Real heading: red line with arrow head (3 m)
        self._real_line = QtWidgets.QGraphicsLineItem()
        real_pen = QtGui.QPen(QtGui.QColor("#ff1744"))
        real_pen.setWidthF(2.2)
        self._real_line.setPen(real_pen)
        self._real_line.setZValue(18)
        self.scene.addItem(self._real_line)
        self._real_line.setVisible(False)

        # Arrow head for real heading
        self._real_arrow = QtWidgets.QGraphicsPathItem()
        self._real_arrow.setPen(real_pen)
        self._real_arrow.setZValue(19)
        self.scene.addItem(self._real_arrow)
        self._real_arrow.setVisible(False)

        # Target heading: green line (5 m), no arrow
        self._targ_line = QtWidgets.QGraphicsLineItem()
        targ_pen = QtGui.QPen(QtGui.QColor("#00e676"))
        targ_pen.setWidthF(2.0)
        self._targ_line.setPen(targ_pen)
        self._targ_line.setZValue(17)
        self.scene.addItem(self._targ_line)
        self._targ_line.setVisible(False)

        # Waypoint katmanı
        self.wp_group = QtWidgets.QGraphicsItemGroup()
        self.wp_group.setZValue(15)
        self.scene.addItem(self.wp_group)

        # Butonlar (Zoom In/Out)
        self.btn_zoom_in = QtWidgets.QToolButton(self)
        self.btn_zoom_in.setText("+")
        self.btn_zoom_in.clicked.connect(lambda: self.zoom(1.25))
        self.btn_zoom_out = QtWidgets.QToolButton(self)
        self.btn_zoom_out.setText("−")
        self.btn_zoom_out.clicked.connect(lambda: self.zoom(0.8))

        # Yerleşim ve ilk konum
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)
        # tıklama ve hareket sinyalleri
        self.view.clicked.connect(self._on_map_click)
        self.view.moved.connect(self._on_mouse_move)

        # tıklanan etiketlerin üst grubu
        self.click_labels_root = QtWidgets.QGraphicsItemGroup()
        self.click_labels_root.setZValue(500)
        self.scene.addItem(self.click_labels_root)

        # imleği netleştirmek için küçük kırmızı + (crosshair)
        self.crosshair = QtWidgets.QGraphicsItemGroup()
        pen = QtGui.QPen(QtGui.QColor("#ff1744"))
        pen.setWidthF(1.6)
        l1 = QtWidgets.QGraphicsLineItem(-5, 0, 5, 0)
        l2 = QtWidgets.QGraphicsLineItem(0, -5, 0, 5)
        for ln in (l1, l2):
            ln.setPen(pen)
            ln.setZValue(1000)
            ln.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
            ln.setAcceptedMouseButtons(Qt.NoButton)  # tıklamayı engellemesin
            self.crosshair.addToGroup(ln)
        self.crosshair.setZValue(1000)
        self.crosshair.setAcceptedMouseButtons(Qt.NoButton)
        self.scene.addItem(self.crosshair)
        self.crosshair.setVisible(True)

        self._btn_box = QtWidgets.QWidget(self)
        self._btn_box.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        hb = QtWidgets.QVBoxLayout(self._btn_box)
        hb.setContentsMargins(6, 6, 6, 6)
        hb.setSpacing(6)
        hb.addWidget(self.btn_zoom_in)
        hb.addWidget(self.btn_zoom_out)
        hb.addStretch()
        self._btn_box.setFixedWidth(44)
        self._btn_box.setStyleSheet("QToolButton{font-size:18px; font-weight:700; padding:6px;}")
        self._label_offset = QtCore.QPointF(8, -8)  # etiketi noktadan ne kadar kaydıracağımız (px)

        self.view.setSceneRect(QtCore.QRectF(0, 0, self._img_W, self._img_H))
        self.center_image()
        self.set_zoom(0.2)

        self._click_labels = []  # [{ "grp":..., "bg_rect":QRectF, "scene_pt":QPointF, "line":QGraphicsLineItem }]
        self.view.zoomChanged.connect(self._relayout_click_labels)

        self._debug_corners()

    def _latlon_from_scene(self, px: float, py: float) -> tuple[float, float]:
        x_left = self._lon_to_x(self.west)
        x_right = self._lon_to_x(self.east)
        y_top = self._lat_to_y(self.north)
        y_bot = self._lat_to_y(self.south)

        W = float(self._img_W)
        H = float(self._img_H)

        x_merc = x_left + (px / W) * (x_right - x_left)
        y_merc = y_top - (py / H) * (y_top - y_bot)

        lon = math.degrees(x_merc / self._R)
        lat = math.degrees(2.0 * math.atan(math.exp(y_merc / self._R)) - math.pi / 2.0)
        return lat, lon

    @QtCore.Slot(QtCore.QPointF)
    def _on_mouse_move(self, scene_pt: QtCore.QPointF):
        # kırmızı + imleci takip etsin
        self.crosshair.setPos(scene_pt)

    def _ascend_to_label_group(self, item: QtWidgets.QGraphicsItem | None):
        it = item
        while it is not None:
            if isinstance(it, QtWidgets.QGraphicsItemGroup) and it.data(0) == "click_label":
                return it
            it = it.parentItem()
        return None

    def _relayout_click_labels(self, *_):
        scale = max(1e-9, self.view.transform().m11())
        ofs_scene = QtCore.QPointF(self._label_offset.x() / scale, self._label_offset.y() / scale)
        for it in list(self._click_labels):
            grp = it.get("grp")
            line = it.get("line")
            scene_pt = it.get("scene_pt")
            bg_rect = it.get("bg_rect")
            if not grp or not line:
                continue
            # etiketi yeniden konumlandır
            grp.setPos(scene_pt + ofs_scene)
            # çizgi başlangıcını sahnede yeniden hesapla
            start_scene = grp.pos() + QtCore.QPointF(bg_rect.left() / scale, bg_rect.top() / scale)
            line.setLine(QtCore.QLineF(start_scene, scene_pt))

    def _add_click_label(self, scene_pt: QtCore.QPointF, lat: float, lon: float):
        # --- METİN ---
        txt = QtWidgets.QGraphicsTextItem(f"{lat:.6f}, {lon:.6f}")
        f = txt.font()
        f.setPointSizeF(8.5)
        f.setBold(True)
        txt.setFont(f)
        txt.setDefaultTextColor(QtGui.QColor("#ffffff"))
        txt.setAcceptedMouseButtons(Qt.NoButton)

        # --- ARKA PLAN ---
        br = txt.boundingRect()
        bg_rect = QtCore.QRectF(-4, -2, br.width() + 8, br.height() + 4)

        class _BgRect(QtWidgets.QGraphicsRectItem):
            def __init__(self, rect, closer):
                super().__init__(rect)
                self._closer = closer

            def mousePressEvent(self, e):
                if e.button() == Qt.LeftButton:
                    self._closer()
                else:
                    super().mousePressEvent(e)

        # --- GRUP (sabit piksel boyutlu etiket) ---
        grp = QtWidgets.QGraphicsItemGroup()
        grp.setOpacity(0.30)
        grp.setZValue(600)
        grp.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)

        bg = _BgRect(bg_rect, closer=lambda: None)
        bg.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0)))
        bg.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0)))

        grp.addToGroup(bg)
        grp.addToGroup(txt)

        # --- POZİSYON: ofseti ölçekle normalize et ---
        scale = max(1e-9, self.view.transform().m11())
        ofs_scene = QtCore.QPointF(self._label_offset.x() / scale, self._label_offset.y() / scale)
        grp.setPos(scene_pt + ofs_scene)
        self.scene.addItem(grp)

        # --- KIRMIZI ÇİZGİ: sol ÜST köşeden piksele ---
        start_scene = grp.pos() + QtCore.QPointF(bg_rect.left() / scale, bg_rect.top() / scale)
        line = self.scene.addLine(QtCore.QLineF(start_scene, scene_pt),
                                  QtGui.QPen(QtGui.QColor("#ff1744"), KIRMIZI_CIZGI_KALINLIK))
        line.setZValue(599)

        # --- KAPATMA ---
        def _close():
            # sahneden sil
            try:
                self.scene.removeItem(line)
            except Exception:
                pass
            try:
                self.scene.removeItem(grp)
            except Exception:
                pass
            # listeden düş
            self._click_labels[:] = [it for it in self._click_labels if it.get("grp") is not grp]

        bg._closer = _close  # etikete tıklayınca hemen kapanır

        # --- 10 sn sonra otomatik kaldır ---
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(_close)
        timer.start(10_000)  # ms

        # listede sakla (zoom’da relayout için)
        self._click_labels.append({
            "grp": grp,
            "bg_rect": QtCore.QRectF(bg_rect),
            "scene_pt": QtCore.QPointF(scene_pt),
            "line": line,
            "timer": timer,
        })

    @QtCore.Slot(QtCore.QPointF, QtCore.QPoint)
    def _on_map_click(self, scene_pt: QtCore.QPointF, global_pos: QtCore.QPoint):
        # Önce tıklanan yerde bir etiket var mı diye bak; varsa kaldır
        item = self.scene.itemAt(scene_pt, self.view.transform())
        grp = self._ascend_to_label_group(item)
        if grp is not None:
            # grubu sahneden kaldır
            self.scene.removeItem(grp)
            return

        # Yoksa yeni etiket oluştur
        lat, lon = self._latlon_from_scene(scene_pt.x(), scene_pt.y())
        self._add_click_label(scene_pt, lat, lon)

    def _debug_corners(self):
        """Harita BBOX köşelerine yeşil çarpı koyarak hizalamayı test eder."""
        # Eski debug objelerini temizle
        try:
            for it in getattr(self, "_debug_items", []):
                self.scene.removeItem(it)
        except Exception:
            pass
        self._debug_items = []

        def cross(p, size=8, color="#00c853"):
            pen = QtGui.QPen(QtGui.QColor(color))
            pen.setWidthF(1.5)
            l1 = QtWidgets.QGraphicsLineItem(p.x() - size, p.y(), p.x() + size, p.y())
            l2 = QtWidgets.QGraphicsLineItem(p.x(), p.y() - size, p.x(), p.y() + size)
            l1.setPen(pen)
            l2.setPen(pen)
            l1.setZValue(30)
            l2.setZValue(30)
            # Zoom’dan bağımsız olsun istiyorsan:
            if globals().get("KEEP_PIXEL_SIZE", True):
                l1.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
                l2.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
            self.scene.addItem(l1)
            self.scene.addItem(l2)
            self._debug_items.extend([l1, l2])

        # NW, NE, SW, SE köşelerine çarpı
        for (lat, lon) in [
            (self.north, self.west), (self.north, self.east),
            (self.south, self.west), (self.south, self.east)
        ]:
            p = self._scene_pos_from_latlon(lat, lon)
            if p is not None:
                cross(p)

    def resizeEvent(self, e: QtGui.QResizeEvent) -> None:
        super().resizeEvent(e)
        # Buton kutusunu sağ-üstte tut
        m = 8
        self._btn_box.move(self.width() - self._btn_box.width() - m, m)

    # ---- Zoom / Pan ----
    def current_zoom(self) -> float:
        m = self.view.transform()
        # scaleX
        return m.m11()

    def set_zoom(self, z: float):
        z = max(self._min_zoom, min(self._max_zoom, z))
        cur = self.current_zoom()
        if cur == 0:
            return
        self.view.scale(z / cur, z / cur)

    def zoom(self, factor: float):
        self.set_zoom(self.current_zoom() * factor)

    def center_image(self):
        self.view.setSceneRect(QtCore.QRectF(0, 0, self._img_W, self._img_H))
        self.view.centerOn(self._img_W / 2.0, self._img_H / 2.0)

    # ---- Lat/Lon -> Scene koordinat dönüşümü ----
    def _lon_to_x(self, lon_deg: float) -> float:
        return self._R * math.radians(lon_deg)

    def _lat_to_y(self, lat_deg: float) -> float:
        # WebMercator (EPSG:3857)
        lat_rad = math.radians(lat_deg)
        return self._R * math.log(math.tan(math.pi/4.0 + lat_rad/2.0))

    def _scene_pos_from_latlon(self, lat: float, lon: float) -> QtCore.QPointF | None:
        # Dışarıda kalanı çizme
        if not (self.south <= lat <= self.north and self.west <= lon <= self.east):
            # Yine de harita dışında olsa bile piksele dönüştürmek isteyebilirsin.
            # Burada clamp'leyelim ki kenarda görünsün.
            lat = min(max(lat, self.south), self.north)
            lon = min(max(lon, self.west), self.east)

        x_left  = self._lon_to_x(self.west)
        x_right = self._lon_to_x(self.east)
        y_top   = self._lat_to_y(self.north)
        y_bot   = self._lat_to_y(self.south)

        x = self._lon_to_x(lon)
        y = self._lat_to_y(lat)

        # Pixmap boyutu
        W = float(self._img_W)
        H = float(self._img_H)

        # WebMercator yukarı pozitif; QGraphics'te y aşağı pozitif.
        # Bu nedenle y'yi ters çeviriyoruz.
        px = (x - x_left) / (x_right - x_left) * W
        py = (y_top - y)   / (y_top - y_bot)  * H

        return QtCore.QPointF(px, py)

    # ---- Geo helpers ----
    def _offset_latlon(self, lat_deg: float, lon_deg: float, north_m: float, east_m: float) -> tuple[float, float]:
        """Küçük ofset yaklaşıklığı. north_m: +Kuzey, east_m: +Doğu (metre)."""
        R = 6378137.0
        d_lat = (north_m / R) * (180.0 / math.pi)
        d_lon = (east_m / (R * math.cos(math.radians(lat_deg)))) * (180.0 / math.pi)
        return lat_deg + d_lat, lon_deg + d_lon

    def _end_latlon_from_heading(self, lat: float, lon: float, heading_deg: float, length_m: float) -> tuple[
        float, float]:
        """0° = True North, saat yönü (+). length_m kadar ilerleyince uç lat/lon."""
        theta = math.radians(heading_deg if heading_deg is not None else 0.0)
        north_m = length_m * math.cos(theta)
        east_m = length_m * math.sin(theta)
        return self._offset_latlon(lat, lon, north_m, east_m)

    def _update_heading_items(self):
        lat = self._last_lat;
        lon = self._last_lon
        if lat is None or lon is None:
            self._real_line.setVisible(False)
            self._real_arrow.setVisible(False)
            self._targ_line.setVisible(False)
            return

        # --- Real heading (3 m + ok) ---
        if self._real_hdg_deg is not None:
            lat2, lon2 = self._end_latlon_from_heading(lat, lon, self._real_hdg_deg, 3.0)
            p1 = self._scene_pos_from_latlon(lat, lon)
            p2 = self._scene_pos_from_latlon(lat2, lon2)
            self._real_line.setLine(p1.x(), p1.y(), p2.x(), p2.y())
            self._real_line.setVisible(True)

            # Ok başı: uca yakın V şekli (±25°)
            arrow_len = 0.8  # m
            left_dir = (self._real_hdg_deg + 180 - 25) % 360
            right_dir = (self._real_hdg_deg + 180 + 25) % 360
            latL, lonL = self._end_latlon_from_heading(lat2, lon2, left_dir, arrow_len)
            latR, lonR = self._end_latlon_from_heading(lat2, lon2, right_dir, arrow_len)
            pL = self._scene_pos_from_latlon(latL, lonL)
            pR = self._scene_pos_from_latlon(latR, lonR)

            path = QtGui.QPainterPath()
            path.moveTo(pL)
            path.lineTo(p2)
            path.lineTo(pR)
            self._real_arrow.setPath(path)
            self._real_arrow.setVisible(True)
        else:
            self._real_line.setVisible(False)
            self._real_arrow.setVisible(False)

        # --- Target heading (5 m, ok yok) ---
        if self._targ_hdg_deg is not None:
            lat3, lon3 = self._end_latlon_from_heading(lat, lon, self._targ_hdg_deg, 5.0)
            p1 = self._scene_pos_from_latlon(lat, lon)
            p3 = self._scene_pos_from_latlon(lat3, lon3)
            self._targ_line.setLine(p1.x(), p1.y(), p3.x(), p3.y())
            self._targ_line.setVisible(True)
        else:
            self._targ_line.setVisible(False)

    # ---- Public setters ----
    def set_real_heading(self, hdg_deg: float | None):
        try:
            self._real_hdg_deg = float(hdg_deg) if hdg_deg is not None else None
        except Exception:
            self._real_hdg_deg = None
        self._update_heading_items()

    def set_target_heading(self, hdg_deg: float | None):
        try:
            self._targ_hdg_deg = float(hdg_deg) if hdg_deg is not None else None
        except Exception:
            self._targ_hdg_deg = None
        self._update_heading_items()

    # ---- Güncellemeler ----
    def update_current_position(self, lat: float, lon: float, draw_tail: bool = True):
        self._last_lat, self._last_lon = float(lat), float(lon)
        p = self._scene_pos_from_latlon(lat, lon)
        if p is None:
            return
        self.curr_marker.setPos(p)
        if not self.curr_marker.isVisible():
            self.curr_marker.setVisible(True)

        if draw_tail:
            if self.path_path.isEmpty():
                self.path_path.moveTo(p)
            else:
                self.path_path.lineTo(p)
            self.path_item.setPath(self.path_path)
            # Konum değişince heading overlayleri tazele
            self._update_heading_items()

    def set_waypoints(self, gps_dict: dict):
        """gps_dict beklenen biçim: {"GPS1":{"lat":..,"lon":..}, ...}"""
        if not isinstance(gps_dict, dict):
            return

        # Eski öğeleri temizle
        try:
            for item in list(self.wp_group.childItems()):
                self.scene.removeItem(item)
            # Grubu yeniden yarat (bazı Qt sürümlerinde childItems temizlenince grup 'tuhaf' davranabiliyor)
            self.scene.removeItem(self.wp_group)
        except Exception:
            pass

        self.wp_group = QtWidgets.QGraphicsItemGroup()
        self.wp_group.setZValue(15)
        self.scene.addItem(self.wp_group)

        # Boyut/sabitler (yoksa varsayılanları kullan)
        r_px = globals().get("WP_RADIUS_PX", 4.0)
        font_pt = globals().get("WP_FONT_PT", 8.5)
        keep_px = globals().get("KEEP_PIXEL_SIZE", True)

        # GPS1..GPS5 sırayla çiz
        for i in range(1, 6):
            key = f"GPS{i}"
            entry = gps_dict.get(key)
            if not isinstance(entry, dict):
                continue
            try:
                lat = float(entry.get("lat"))
                lon = float(entry.get("lon"))
            except Exception:
                continue
            if lat is None or lon is None:
                continue

            p = self._scene_pos_from_latlon(lat, lon)
            if p is None:
                continue

            # --- Daire marker ---
            circ = QtWidgets.QGraphicsEllipseItem(-r_px, -r_px, 2 * r_px, 2 * r_px)
            circ.setBrush(QtGui.QBrush(QtGui.QColor("#1e88e5")))
            circ.setPen(QtGui.QPen(QtGui.QColor("#ffffff"), 1.0))
            circ.setPos(p)
            circ.setZValue(16)
            if keep_px:
                circ.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)
            self.wp_group.addToGroup(circ)

            # --- Etiket ---
            label = QtWidgets.QGraphicsTextItem(key)
            # Yazı tipi ve stil
            f = label.font()
            f.setPointSizeF(font_pt)
            f.setBold(True)
            label.setFont(f)

            # Renk: turkuaz
            label.setDefaultTextColor(QtGui.QColor("#90EE90")) #000000 #30d5c8

            # Metni DAİRENİN MERKEZİNE yerleştir
            br = label.boundingRect()  # font set edildikten sonra ölç
            label.setPos(p - QtCore.QPointF(br.width() / 2.0, br.height() / 2.0))

            label.setZValue(17)
            if keep_px:
                label.setFlag(QtWidgets.QGraphicsItem.ItemIgnoresTransformations, True)

            self.wp_group.addToGroup(label)


# -------------------- Main Window --------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("DHO KEMALREIS - YER KONTROL")
        icon_path = res_path("cross.ico").replace("\\", "/")
        self.setWindowIcon(QtGui.QIcon(icon_path))

        self.resize(1120, 720)

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)

        # Yatay splitter: sol (kontroller) | orta (harita) | sağ (video)
        splitter = QtWidgets.QSplitter(Qt.Horizontal, central)

        # ---- HALAT ÇERÇEVE ----
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.rope = QtWidgets.QFrame(central)
        self.rope.setObjectName("ropeFrame")
        root.addWidget(self.rope)

        ROPE = 12  # halat kalınlığı (px) — resmine göre 24/32/48 deneyebilirsin
        self._rope_thk = ROPE  # tek kaynak
        self.content = QtWidgets.QFrame(central)
        root.addWidget(self.content)

        content_layout = QtWidgets.QHBoxLayout(self.content)
        content_layout.setContentsMargins(ROPE, ROPE, ROPE, ROPE)  # İçeriği halattan içeri al
        content_layout.setSpacing(0)
        content_layout.addWidget(splitter)  # sol panel + harita zaten splitter'a ekleniyor

        # halat görseli
        rope_path = res_path("halat.png").replace("\\", "/")
        # --- Dört kenar şeridi (overlay) ---
        self.rope_top = RopeStripe("top", rope_path, ROPE, parent=self.content)
        self.rope_bottom = RopeStripe("bottom", rope_path, ROPE, parent=self.content)
        self.rope_left = RopeStripe("left", rope_path, ROPE, parent=self.content)
        self.rope_right = RopeStripe("right", rope_path, ROPE, parent=self.content)
        self.content.installEventFilter(self)  # resize'da yerleşim için

        # ---- Sol Panel (eski kontroller) ----
        left = QtWidgets.QWidget()
        left.setObjectName("leftPanel")
        left.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        bg_path = res_path("background.png").replace("\\", "/")

        left.setStyleSheet(f'''
        #leftPanel {{
            border-image: url("{bg_path}") 0 0 0 0 stretch stretch;
        }}
        ''')

        v = QtWidgets.QVBoxLayout(left)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(8)

        # Connection row
        row = QtWidgets.QHBoxLayout(); v.addLayout(row)
        self.cmb_port = QtWidgets.QComboBox(); self._refresh_ports()
        row.addWidget(QtWidgets.QLabel("COM:")); row.addWidget(self.cmb_port)
        self.cmb_baud = QtWidgets.QComboBox()
        [self.cmb_baud.addItem(str(b)) for b in (57600, 115200, 38400, 230400)]
        self.cmb_baud.setCurrentText("57600")
        row.addWidget(QtWidgets.QLabel("Baud:")); row.addWidget(self.cmb_baud)
        self.chk_csv = QtWidgets.QCheckBox("CSV log"); row.addWidget(self.chk_csv)
        self.btn_refresh = QtWidgets.QPushButton("Refresh")
        self.btn_refresh.clicked.connect(self._refresh_ports); row.addWidget(self.btn_refresh)
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_connect.clicked.connect(self._toggle); row.addWidget(self.btn_connect)

        # Telemetry grid
        grid = QtWidgets.QGridLayout(); v.addLayout(grid)
        self.val_pwm_l = self._pair(grid, 0, 0, "SOL PWM:")
        self.val_pwm_r = self._pair(grid, 0, 2, "SAĞ PWM:")
        self.val_speed = self._pair(grid, 1, 0, "HIZ (m/s):")
        self.val_hdg = self._pair(grid, 1, 2, "GERÇEK HDG (°):")
        self.val_hdg_t = self._pair(grid, 2, 0, "HEDEF HDG (°):")
        self.val_hdg_health = self._pair(grid, 2, 2, "HDG SAĞLIĞI:")
        self.val_next = self._pair(grid, 3, 0, "SONRAKİ NOKTA:")
        self.val_dist = self._pair(grid, 3, 2, "KALAN (m):")
        self.val_pos = self._pair(grid, 4, 0, "MEVCUT KONUM:")
        self.val_manual = self._pair(grid, 4, 2, "MANUEL MOD:")
        self.val_time = self._pair(grid, 5, 0, "ZAMAN (t_ms):")
        self.val_fps = self._pair(grid, 5, 2, "FPS:")

        # GPS table (editable senders)
        gps_box = QtWidgets.QGroupBox("GPS Noktaları (canlı görüntü + güncelle)")
        v.addWidget(gps_box)
        g = QtWidgets.QGridLayout(gps_box)
        self.gps_edits = {}
        # YENİ: dinleme durumları (varsayılan True) ve checkbox referansları
        self.gps_listen = {i: True for i in range(1, 6)}
        self.gps_listen_chk = {}

        for i in range(1, 6):
            g.addWidget(QtWidgets.QLabel(f"GPS{i} lat"), i - 1, 0)
            e_lat = QtWidgets.QLineEdit(); e_lat.setPlaceholderText("lat"); g.addWidget(e_lat, i - 1, 1)
            g.addWidget(QtWidgets.QLabel(f"GPS{i} lon"), i - 1, 2)
            e_lon = QtWidgets.QLineEdit(); e_lon.setPlaceholderText("lon"); g.addWidget(e_lon, i - 1, 3)
            btn = QtWidgets.QPushButton(f"Gönder GPS{i}")
            btn.clicked.connect(lambda _, idx=i: self._send_gps(idx)); g.addWidget(btn, i - 1, 4)
            self.gps_edits[i] = (e_lat, e_lon)

            # YENİ: “dinle” kutusu — telemetriyi dinle/dinleme
            chk = QtWidgets.QCheckBox("dinle")
            chk.setChecked(True)
            chk.setToolTip("Tikli: telemetri bu GPS’i günceller. Tiksiz: telemetriyi yok sayar (TX devam eder).")
            chk.toggled.connect(lambda state, idx=i: self._set_gps_listen(idx, state))
            g.addWidget(chk, i - 1, 5)
            self.gps_listen_chk[i] = chk

        # Manual/Autonomy controls
        ctrl_row = QtWidgets.QHBoxLayout(); v.addLayout(ctrl_row)
        self.btn_manual = QtWidgets.QPushButton("Manual Mode ON")
        self.btn_manual.clicked.connect(lambda: self._send_set_manual(True)); ctrl_row.addWidget(self.btn_manual)
        self.btn_auto = QtWidgets.QPushButton("Start Autonomy (Manual OFF)")
        self.btn_auto.clicked.connect(lambda: self._send_set_manual(False)); ctrl_row.addWidget(self.btn_auto)

        # ---- EMERGENCY STOP ----
        em_row = QtWidgets.QHBoxLayout(); v.addLayout(em_row)
        self.btn_emergency = QtWidgets.QPushButton("🛑 ACİL SİSTEM KAPATMA")
        self.btn_emergency.setStyleSheet(
            "background-color:#ff1744; color:white; font-weight:900; "
            "font-size:16px; border-radius:8px; padding:12px 16px;"
        )
        self.btn_emergency.setMinimumHeight(52)
        self.btn_emergency.clicked.connect(self._send_emergency_stop)
        em_row.addWidget(self.btn_emergency)

        # Direction buttons
        dir_box = QtWidgets.QGroupBox("Manual PWM (ok tuşlarıyla da çalışır)")
        v.addWidget(dir_box)
        d = QtWidgets.QGridLayout(dir_box)
        self.btn_up = QtWidgets.QPushButton("↑ ileri (1600/1600)")
        self.btn_up.clicked.connect(lambda: self._send_manual_pwm(1600, 1600)); d.addWidget(self.btn_up, 0, 1)
        self.btn_left = QtWidgets.QPushButton("← sol (1300/1600)")
        self.btn_left.clicked.connect(lambda: self._send_manual_pwm(1300, 1600)); d.addWidget(self.btn_left, 1, 0)
        self.btn_stop = QtWidgets.QPushButton("■ STOP (1500/1500)")
        self.btn_stop.clicked.connect(lambda: self._send_manual_pwm(1500, 1500))
        self.btn_stop.setStyleSheet("background-color:#c62828; color:white; font-weight:700; border-radius:6px;")
        self.btn_stop.setMinimumHeight(40); d.addWidget(self.btn_stop, 1, 1)
        self.btn_right = QtWidgets.QPushButton("→ sağ (1600/1300)")
        self.btn_right.clicked.connect(lambda: self._send_manual_pwm(1600, 1300)); d.addWidget(self.btn_right, 1, 2)
        self.btn_down = QtWidgets.QPushButton("↓ geri (1300/1300)")
        self.btn_down.clicked.connect(lambda: self._send_manual_pwm(1300, 1300)); d.addWidget(self.btn_down, 2, 1)

        # Log box
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True); self.log.setMaximumBlockCount(300)
        v.addWidget(self.log, 1)

        # ---- Orta Panel (Harita) ----
        self.map = MapPanel(MAP_IMAGE_PATH, NORTH, SOUTH, WEST, EAST)

        # ---- Sağ Panel (Video) ----
        self.video_panel = QtWidgets.QWidget()
        self.video_panel.setStyleSheet("background-color: #222; border-left: 2px solid #444;")
        v_layout = QtWidgets.QVBoxLayout(self.video_panel)

        lbl_cam_title = QtWidgets.QLabel("ASV LIVE CAM1")
        lbl_cam_title.setStyleSheet("color: white; font-weight: bold; font-size: 14px;")
        lbl_cam_title.setAlignment(Qt.AlignCenter)
        v_layout.addWidget(lbl_cam_title)

        # Görüntünün basılacağı etiket
        self.lbl_video = QtWidgets.QLabel()
        self.lbl_video.setAlignment(Qt.AlignCenter)
        self.lbl_video.setStyleSheet("background-color: #000;")
        self.lbl_video.setMinimumSize(320, 240)
        v_layout.addWidget(self.lbl_video)
        v_layout.addStretch()  # Alt boşluk

        # Splitter'a ekle: Sol - Harita - Video
        splitter.addWidget(left)
        splitter.addWidget(self.map)
        splitter.addWidget(self.video_panel)  # <--- YENİ EKLENDİ

        # Genişlik oranları (Sol:Sabit, Harita:Geniş, Video:Orta)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 1)

        # ---- Video Thread Başlatma ----
        self.video_thread = VideoWorker(port=5000)  # İDA'nın yolladığı port
        self.video_thread.frame_signal.connect(self.update_video_frame)
        self.video_thread.start()

        self.worker: Optional[SerialWorker] = None
        self._connected = False
        self._manual_mode = False
        self.setFocusPolicy(Qt.StrongFocus)

        # --- GPS'leri 5 sn'de bir uygulamak için ---
        self._latest_gps = None
        self._gps_timer = QtCore.QTimer(self)
        self._gps_timer.timeout.connect(self._apply_latest_gps)
        self._gps_timer.start(GPS_UPDATE_INTERVAL_MS)

        self._layout_rope_stripes()

    def _set_gps_listen(self, idx: int, ok: bool):
        """GPS idx için telemetri dinleme bayrağını güncelle."""
        self.gps_listen[idx] = bool(ok)
        # Not: Alanları devre dışı bırakmıyoruz; kullanıcı yine elle düzenleyip TX gönderebilsin.

    def _layout_rope_stripes(self):
        t = self._rope_thk
        w = self.content.width()
        h = self.content.height()
        self.rope_top.setGeometry(0, 0, w, t)
        self.rope_bottom.setGeometry(0, h - t, w, t)
        self.rope_left.setGeometry(0, 0, t, h)
        self.rope_right.setGeometry(w - t, 0, t, h)

    def eventFilter(self, obj, event):
        # Zaten left panel için eventFilter kullanıyorsun; bunu bozmadan genişletiyoruz
        if obj is getattr(self, "content", None) and event.type() == QtCore.QEvent.Resize:
            self._layout_rope_stripes()
        # (Var olan left panel filtren kalsın)
        if obj is getattr(self, "left", None) and event.type() == QtCore.QEvent.Resize:
            self.left_overlay.setGeometry(self.left.rect())
        return super().eventFilter(obj, event)

    # ---- UI helpers ----
    def _pair(self, grid: QtWidgets.QGridLayout, r: int, c: int, title: str) -> QtWidgets.QLabel:
        grid.addWidget(QtWidgets.QLabel(title), r, c)
        val = QtWidgets.QLabel("-")
        val.setStyleSheet("font-weight: 600")
        grid.addWidget(val, r, c + 1)
        return val

    def _refresh_ports(self):
        self.cmb_port.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]

        def _num(x: str) -> int:
            try:
                if x.startswith("COM"):
                    return int(x[3:])
            except Exception:
                return 0
            return 0

        ports.sort(key=_num, reverse=True)
        if not ports:
            ports = ["COM3"]
        self.cmb_port.addItems(ports)

    # ---- Connect/Disconnect ----
    def _toggle(self):
        if not self._connected:
            port = self.cmb_port.currentText().strip()
            baud = int(self.cmb_baud.currentText())
            csv_path = "telemetry_log.csv" if self.chk_csv.isChecked() else None
            self.worker = SerialWorker(port, baud, csv_path)
            self.worker.packet.connect(self.on_packet)
            self.worker.status.connect(self.on_status)
            self.worker.link.connect(self.on_link)
            self.worker.start()
            self._ping_timer = QtCore.QTimer(self)
            self._ping_timer.timeout.connect(lambda: self.worker and self.worker.queue_send({"cmd": "ping"}))
            self._ping_timer.start(5000)
            self._connected = True
            self.btn_connect.setText("Disconnect")
            self.on_status(f"Connecting {port} @ {baud}...")
        else:
            if self.worker:
                self.worker.stop()
                self.worker.wait(1500)
                self.worker = None
            self._connected = False
            self.btn_connect.setText("Connect")
            self.on_status("Disconnected.")

    # ---- Sadece GPS'i 5 sn'de bir uygula ----
    def _apply_latest_gps(self):
        gps = self._latest_gps
        if not isinstance(gps, dict):
            return

        draw = {}  # haritaya çizilecekler (sadece dinlenenler)

        # Soldaki GPS editlerini (focus’ta değilse) güncelle — fakat sadece dinlenenler
        for i in range(1, 6):
            name = f"GPS{i}"
            if name in gps and self.gps_listen.get(i, True):
                lat = gps[name].get("lat")
                lon = gps[name].get("lon")
                e_lat, e_lon = self.gps_edits[i]
                if not e_lat.hasFocus():
                    e_lat.setText("" if lat is None else str(lat))
                if not e_lon.hasFocus():
                    e_lon.setText("" if lon is None else str(lon))
                # Harita için de biriktir
                if lat is not None and lon is not None:
                    draw[name] = {"lat": lat, "lon": lon}

        # Haritadaki waypoint’leri 5 sn’de bir çiz (sadece dinlenenler)
        try:
            self.map.set_waypoints(draw)
        except Exception as e:
            self.on_status(f"wp draw err: {e}")

    # ---- Telemetry handling ----
    @QtCore.Slot(dict)
    def on_packet(self, d: dict):

        # Telemetri alanları: ANLIK güncelle
        self.val_pwm_l.setText(str(d.get("SOL_İTİCİ_İTKİ_İSTEĞİ_PWM", "-")))
        self.val_pwm_r.setText(str(d.get("SAĞ_İTİCİ_İTKİ_İSTEĞİ_PWM", "-")))
        self.val_speed.setText(self._fmt_float(d.get("İDA_GERÇEK_HIZ_mps")))
        self.val_hdg.setText(self._fmt_float(d.get("GERÇEK_HEADING_deg")))
        self.val_hdg_t.setText(self._fmt_float(d.get("HEDEF_HEADING_deg")))
        try:
            _hdg_real = d.get("GERÇEK_HEADING_deg")
            _hdg_targ = d.get("HEDEF_HEADING_deg")
            if self.map:
                self.map.set_real_heading(float(_hdg_real) if _hdg_real is not None else None)
                self.map.set_target_heading(float(_hdg_targ) if _hdg_targ is not None else None)
        except Exception:
            pass
        self.val_hdg_health.setText(str(d.get("HEADING_SAĞLIĞI", "-")))
        self.val_next.setText(str(d.get("SONRAKİ_GÖREV_NOKTASI", "-")))
        self.val_dist.setText(self._fmt_float(d.get("KALAN_MESAFE_m")))
        pos = d.get("MEVCUT_KONUM")
        if isinstance(pos, dict):
            lat = pos.get("lat"); lon = pos.get("lon")
            self.val_pos.setText(f"{lat} , {lon}")
            try:
                if lat is not None and lon is not None:
                    self.map.update_current_position(float(lat), float(lon), draw_tail=True)
            except Exception:
                pass
        else:
            self.val_pos.setText("-")
        self._manual_mode = bool(d.get("MANUEL_MOD"))
        self.val_manual.setText(str(self._manual_mode))
        self.val_time.setText(str(d.get("t_ms", "-")))
        self.val_fps.setText(str(d.get("FPS", d.get("fps", "-"))))

        # GPS verisini SADECE CACHE’LE; uygulamayı timer yapacak
        gps_raw = d.get("GÖREV_NOKTALARI") or d.get("GPS_NOKTALARI") or d.get("GOREV_NOKTALARI")
        gps_norm = normalize_gps_dict(gps_raw) if gps_raw else None
        if isinstance(gps_norm, dict):
            self._latest_gps = gps_norm

    def _fmt_float(self, v) -> str:
        try:
            if v is None:
                return "-"
            f = float(v)
            return f"{f:.1f}"
        except Exception:
            return str(v)

    @QtCore.Slot(str)
    def on_status(self, s: str):
        self.log.appendPlainText(s)

    @QtCore.Slot(bool)
    def on_link(self, ok: bool):
        self.log.appendPlainText("[LINK] Connected" if ok else "[LINK] Lost")

    @QtCore.Slot(QtGui.QImage)
    def update_video_frame(self, qt_img):
        # Etiketin boyutuna göre ölçekle (oranı koruyarak)
        w = self.lbl_video.width()
        h = self.lbl_video.height()
        pixmap = QtGui.QPixmap.fromImage(qt_img)
        self.lbl_video.setPixmap(pixmap.scaled(w, h, Qt.KeepAspectRatio))

    # ---- Command senders ----
    def _send_set_manual(self, value: bool):
        if not self.worker: return
        self.worker.queue_send({"cmd": "set_manual", "value": bool(value)})
        self.on_status(f"TX: set_manual -> {value}")

    def _send_gps(self, idx: int):
        if not self.worker:
            # Çalışma yoksa bile yerel olarak gösterelim
            pass
        e_lat, e_lon = self.gps_edits[idx]
        try:
            lat = float(e_lat.text().strip())
            lon = float(e_lon.text().strip())
        except Exception:
            self.on_status(f"GPS{idx} parse error")
            return

        # Cihaza gönder
        if self.worker:
            self.worker.queue_send({"cmd": "set_gps", "index": idx, "lat": lat, "lon": lon})
            self.on_status(f"TX: set_gps {idx} -> ({lat}, {lon})")

        # --- Haritada ANINDA göster ---
        current = {}
        for i in range(1, 6):
            elat, elon = self.gps_edits[i]
            try:
                lat_i = float(elat.text().strip())
                lon_i = float(elon.text().strip())
                current[f"GPS{i}"] = {"lat": lat_i, "lon": lon_i}
            except Exception:
                # boş bırakılanlar atlanır
                pass

        if current:
            try:
                self.map.set_waypoints(current)
            except Exception as e:
                self.on_status(f"local wp draw err: {e}")

    # ---- Arrow keys ----
    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Up:
            self._send_manual_pwm(1600, 1600); e.accept(); return
        if e.key() == Qt.Key_Down:
            self._send_manual_pwm(1300, 1300); e.accept(); return
        if e.key() == Qt.Key_Left:
            self._send_manual_pwm(1300, 1600); e.accept(); return
        if e.key() == Qt.Key_Right:
            self._send_manual_pwm(1600, 1300); e.accept(); return
        if e.key() in (Qt.Key_Space, Qt.Key_S):  # STOP
            self._send_manual_pwm(1500, 1500); e.accept(); return
        super().keyPressEvent(e)

    def closeEvent(self, ev):
        try:
            if self.worker:
                self.worker.stop()
                self.worker.wait(1500)
            if hasattr(self, 'video_thread'):
                self.video_thread.stop()
                self.video_thread.wait(500)
        except Exception:
            pass
        ev.accept()

    def _send_emergency_stop(self):
        if not self.worker:
            self.on_status("Emergency: not connected.")
            return
        mb = QtWidgets.QMessageBox(self)
        mb.setIcon(QtWidgets.QMessageBox.Warning)
        mb.setWindowTitle("Acil Kapatma Onayı")
        mb.setText("Bu işlem uzaktaki sistemi DERHAL kapatır.\nDevam etmek istiyor musun?")
        mb.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        mb.setDefaultButton(QtWidgets.QMessageBox.No)
        ret = mb.exec()
        if ret != QtWidgets.QMessageBox.Yes:
            self.on_status("Emergency stop kullanıcı tarafından iptal edildi.")
            return
        try:
            self.worker.queue_send({"cmd": "emergency_stop"})
            self.on_status("TX: EMERGENCY_STOP gönderildi.")
        except Exception as e:
            self.on_status(f"Emergency send error: {e}")


def main():
    app = QtWidgets.QApplication(sys.argv)
    icon_path = res_path("cross.ico").replace("\\", "/")
    app.setWindowIcon(QtGui.QIcon(icon_path))

    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()