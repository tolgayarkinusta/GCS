# YKI_BIRLESIK → Windows `.exe` (One-File) Paketleme Kılavuzu

Bu doküman, **YKI_BIRLESIK.py** dosyasını **tek dosya (`--onefile`) .exe** haline getirmeniz için adım adım rehberdir. Tüm görseller ve ikon **.exe** içine gömülür, çalışma anında PyInstaller tarafından geçici klasöre çıkarılır.

---

## 1) Proje yapısı (aynı klasörde)

```
YKI_BIRLESIK.py
12.jpg
background.png
halat.png
cross.ico   (32×32 önerilir)
```

> İsimler birebir böyle olmalı (komutta ve kodda bunları kullanacağız).

---

## 2) Onefile uyumlu dosya yolu yardımcı fonksiyonu

Onefile modunda kaynak dosyalar `_MEIPASS` adlı temp klasöre çıkar. Kodunuzda **mutlaka** bu yardımcıyı kullanın:

```python
import sys, os

def res_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(__file__))
    return os.path.join(base, name)
```

Ve tüm dosya yollarını buradan alın:

```python
MAP_IMAGE_PATH = res_path("12.jpg")
bg_path   = res_path("background.png").replace("\", "/")
rope_path = res_path("halat.png").replace("\", "/")
icon_path = res_path("cross.ico").replace("\", "/")

# Pencere/görev çubuğu ikonu
# self.setWindowIcon(QtGui.QIcon(icon_path))     # MainWindow.__init__ içinde
# app.setWindowIcon(QtGui.QIcon(icon_path))      # main() içinde
```

> **Neden gerekli?** Onefile’da dosyalar script klasöründe değil, PyInstaller’ın temp klasöründedir. Bu yardımcı hem onefile hem de normal çalışmada doğru yolu verir.

---

## 3) Sanal ortam ve bağımlılıklar

PowerShell:
```powershell
py -m venv .venv
.\.venv\Scriptsctivate
pip install --upgrade pip
pip install PySide6 pyserial pyinstaller
```
> İsteğe bağlı: UPX kullanacaksanız (ek sıkıştırma) Chocolatey ile `choco install upx -y` kurabilir ve komutta `--upx-dir` verebilirsiniz.

---

## 4) Derleme (PowerShell, onefile)

Aşağıdaki komut **tek .exe** üretir ve gerekli Qt eklentilerini **minimal** olarak ekler. UPX yüklüyse DLL’leri sıkıştırır.

> **Not:** `<venv>` yolunu kendi sanal ortamınızdaki **PySide6 plugins** yoluyla değiştirin. Sizde:
> `C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins`

**PowerShell (çok satır):**
```powershell
(.venv) PS C:\Users\mcoze\PycharmProjects\PythonProject\.venv> py -m PyInstaller --noconfirm --clean --name DHO_Kemalreis --onefile --windowed --icon cross.ico `
  --add-data "12.jpg;." --add-data "background.png;." --add-data "halat.png;." --add-data "cross.ico;." `
  --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll;platforms" `
  --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/imageformats/qjpeg.dll;imageformats" `
  --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/imageformats/qico.dll;imageformats" `
  --hidden-import=serial.win32 --hidden-import=serial.tools.list_ports --hidden-import=serial.tools.list_ports_windows `
  --upx-dir C:\tools\upx `
  YKI_BIRLESIK.py
```

**CMD (tek satır) sürümü:**
```cmd
py -m PyInstaller --noconfirm --clean --name DHO_Kemalreis --onefile --windowed --icon cross.ico --add-data "12.jpg;." --add-data "background.png;." --add-data "halat.png;." --add-data "cross.ico;." --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/platforms/qwindows.dll;platforms" --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/imageformats/qjpeg.dll;imageformats" --add-binary "C:/Users/mcoze/PycharmProjects/PythonProject/.venv/Lib/site-packages/PySide6/plugins/imageformats/qico.dll;imageformats" --hidden-import=serial.win32 --hidden-import=serial.tools.list_ports --hidden-import=serial.tools.list_ports_windows --upx-dir C:\tools\upx YKI_BIRLESIK.py
```

**Açıklamalar:**
- `--onefile` tek `.exe` üretir.  
- `--windowed` konsol penceresini gizler (GUI uygulaması).  
- `--icon cross.ico` → **EXE dosyasının** Explorer ikonu (gömülü).  
- `--add-data` → resimler ve `.ico` .exe içine eklenir.  
- `--add-binary` → Qt **platforms** ve **imageformats** eklenti DLL’leri (Windows’ta şart).  
- `--hidden-import` → pyserial’ın Windows alt modüllerini güvenceye alır.  
- `--upx-dir` → UPX kuruluysa DLL’ler sıkıştırılır (boyut azalır).

---

## 5) Çıktı ve çalıştırma

- Çıktı: `dist\DHO_Kemalreis.exe`  
- `build\` klasörü geçici derleme dosyalarıdır; **çalışma için gerekmez**.  
- `.spec` dosyası tarif dosyasıdır; tekrar derleme için faydalı ama **çalışma için gerekmez**.

---

## 6) Sorun giderme

- **Kaynak dosyalar bulunamıyor**: Kodda tüm yollar **`res_path(...)`** ile mi? `--add-data` girdileri doğru mu?  
- **Qt platform plugin 'windows'** hatası: `platforms/qwindows.dll` ekli mi? Yol doğru mu?  
- **Image/ICO görünmüyor**: Uygulama içinde pencere/görev çubuğu ikonu olarak `QIcon(res_path("cross.ico"))` kullandığınızdan emin olun.  
- **UPX ile final .exe sıkıştırma hatası (CFG)**: Final exe’ye UPX uygulamayın; derleme sırasında `--upx-dir` kullanın.

---

## 7) Boyut küçültme ipuçları (isteğe bağlı)
- **Sadece gereken** plugin’leri ekleyin (yukarıdaki komut zaten minimal).  
- `12.jpg`, `background.png`, `halat.png` dosyalarını optimize edin.  
- Sadece `QtCore/QtGui/QtWidgets` kullanıyorsanız **PySide6-Essentials** deneyin.

---

Artık **tek dosya .exe**’nizi `dist\` klasöründen dağıtabilirsiniz. Başarılar!
