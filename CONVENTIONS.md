# Python Kod Kuralları — MUST

Bu depodaki **her Python dosyası** aşağıdaki kurallara uymak **zorundadır**. Kurallar MUST niteliğindedir; bir kuralı ihlal eden script pipeline'a alınmaz ve merge edilmez.

## 1. Dosya girdili, dosya çıktılı çalışma

- Her script çalışırken **her zaman bir input dosyası alır ve bir output dosyası üretir.**
- Girdi ve çıktı yolları parametre ile verilir (bkz. Kural 5); bu parametreler verilmeden script iş yapmaya başlamaz.
- Script kendi arasında konuşlanmış (interactive) bir araç değildir: okur, işler, yazar, biter.

## 2. Büyük dosyalara dayanıklı ve hızlı I/O

- Dosyalar **asla bir anda belleğe yüklenmez**: `read()`, `readlines()`, `f.read().split()` gibi tümünü-okuma yaklaşımları yasaktır.
- Okuma **satır satır veya parça parça (streaming)** yapılır; gerektiğinde jeneratörler kullanılır.
- Çıktı da **artımlı (incremental)** yazılır; tüm çıktıyı bellekte biriktirip tek seferde yazmaktan kaçınılır.
- Hedef: GB boyutundaki loglarda bile bellek kullanımı sabit ve öngörülebilir kalmalıdır.

## 3. pipeline.py ile sorunsuz tetiklenme

- Her script, `pipeline.py` üzerinden tetiklendiğinde **hiçbir sorun çıkarmadan** çalışabilmelidir.
- Bunun için:
  - **Import edildiğinde yan etki olmaz:** import anında dosya yazımı, network veya LLM çağrısı yapılmaz.
  - **Çağrılabilir giriş noktası sunar:** `main(args)` (veya eşdeğeri) fonksiyonu + `if __name__ == "__main__":` bloğu standarttır.
  - **Etkileşimli giriş kullanılmaz:** `input()` çağrısı yoktur; her şey parametreyle gelir.
  - **Çıkış kodu sözleşmesi:** başarı = `0`, hata = `0` dışı kod + stderr'e insan okur hata mesajı. `pipeline.py` bu koda göre akışı yönetir.

## 4. --debug desteği

- Her script **`--debug` bayrağı ile çalıştırılabilmelidir.**
- `--debug` yoksa normal log seviyesinde (INFO), varsa ayrıntılı seviyede (DEBUG) log üretir.
- Loglar **stderr**'e yazılır; stdout yalnızca programın asıl çıktısı için kullanılır (çıktı pipe edilebilir kalır).

## 5. Tam parametrik çalışma

- Input/output yolları ve belleğe/ortama dair her şey (dizinler, eşik değerleri, model adı, limitler vb.) **komut satırı parametresi** olarak verilebilir olmalıdır.
- Kod içinde gömülü (hardcoded) yol, dosya adı veya ayar değeri barındırılmaz; yalnızca **makul varsayılanlar (default)** tutulabilir.
- Parametre ayrıştırma için `argparse` standarttır; `--help` her zaman çalışır.

## 6. LLM çağrı kaydı (wire log)

- LLM çağrısı yapan her script, **LLM'e giden çağrıyı ve LLM'den dönen cevabı** `./llm` klasörüne yazar (klasör parametrik olarak değiştirilebilir, bkz. Kural 5; varsayılan `./llm`).
- Dosya adlandırma: çağrıyı yapan python script'in adı + sıra numarası ile:
  - `<script_adı>_0001_request.json` — LLM'e giden istek
  - `<script_adı>_0001_response.json` — LLM'den dönen cevap
- Sıra numarası **script başına** 1'den başlar, her çağrıda bir artar. Var olan dosyaların **üzerine yazılmaz**; numaralandırma mevcut en büyük numaradan devam eder (program restart edilse bile sıra bozulmaz).
- Çağrı hata ile sonuçlansa bile response dosyası yazılır (içeriği hata bilgisi olur) — **kayıp çağrı olamaz.**
- Bu kayıtlar denetim izidir (audit trail); hiçbir aşamada sessizce silinmez veya ezilmez.

## 7. Beklemesiz, anında işleme

- Script **bekleme yapmaz:** tüm girdiyi biriktirip sonunda tek seferde işlemek yerine, okuduğu her parçayı **hemen işler ve çıktı dosyasına anında yazmaya başlar.**
- Girdinin tamamının gelmesi/bitmesi, işlemenin ve yazmanın başlaması için ön koşul değildir; oku-işle-yaz döngüsü baştan sona kesintisiz akar.

## 8. Çalışma süresi raporu — report.json

- Her script bir **`report.json`** dosyası oluşturur. İçeriği, geliştirmek için gerekli çalışma bilgileridir; örneğin:
  - hangi aşama ne kadar sürdü (aşama bazlı süreler),
  - işlenen satır/kayıt adetleri ve işleme hızı,
  - hata/atlama sayıları,
  - varsa LLM çağrı adedi ve toplam süresi.
- `report.json` **program bitince değil, çalıştığı sürece sık sık güncel tutulur** (örn. her N satırda bir veya belli aralıklarla). Böylece:
  - uzun koşularda ilerleme dışarıdan gözlenebilir,
  - script ortada çakılırsa/crash olsa bile o ana kadarki rapor kaybolmaz.
- Güncellemeler bozulmaya karşı **atomik** yazılır (geçici dosyaya yaz + üzerine taşı); okuyan taraf asla yarım JSON görmez.
- Dosyanın yolu parametrik olabilir (bkz. Kural 5); varsayılan `report.json`'dur.

## 9. Oturum günlüğü — DEVLOG.md (çalışma düzeni kuralı)

Bu bir python-dosyası kuralı değil, çalışma düzeni kuralıdır; ama aynı derecede MUST'tur:

- Her ZCode oturumunda yapılan işler, repo kökündeki **`DEVLOG.md`** dosyasına **tarihli bir başlık altında eklenerek** özetlenir.
- DEVLOG.md'deki var olan girdiler **asla değiştirilmez veya silinmez** — yalnızca alta yeni giriş eklenir.
- Amaç: oturumlar arası bağlam kaybını önlemek. "Ne yapmıştık, nerede kalmıştık?" sorusunun tek doğruluk kaynağı DEVLOG.md'dir.
- Her oturum DEVLOG.md okunarak başlar, yapılan işler eklenerek kapanır.

## 10. Her şey proje klasöründe — konteyner sıfırdan kurulabilir (çalışma düzeni kuralı)

- Bu projede oluşturulan **her şey** (scriptler, konfigürasyonlar, çıktılar, veriler) Windows `D:` üzerindeki proje klasöründe tutulur: `D:\projeler\SRE-FOR-ALL` = konteyner içinde `/workspace`.
- Konteynerin kendi deposuna (`/home/dev` altı vb.) **kalıcı hiçbir şey konmaz** — orası geçici/atılabilir alandır.
- Ölçüt: "docker'ı baştan oluştur" dendiğinde konteyner silinip `docker compose up -d --build` ile yeniden kurulabilmeli ve **her şey aynen çalışmaya devam etmeli**. Proje klasörü tek başına yeterlidir; konteyner, her zaman yeniden kurulabilir bir ayrıntıdır.
- Bir şeyin nereye gideceği belirsizse varsayılan yer: proje klasörü.

## 11. Modül dokümantasyonu — MODULES.md (çalışma düzeni kuralı)

- Oluşturulan veya değiştirilen **her python modülü, aynı oturumda** repo kökündeki **`MODULES.md`** dosyasına işlenir: amaç, girdi/çıktı, parametreler, örnek komut, test durumu.
- Yeni oturum DEVLOG.md ile birlikte **MODULES.md'i de okuyarak** başlar; modül detayları sohbet hafızasından beklenmez, kimse hatırlatmak zorunda kalmaz.
- **MUST:** modül kodlandı/degisti ama MODULES.md'e yazılmadıysa o iş tamamlanmış sayılmaz.
- DEVLOG'dan farkı: DEVLOG tarihli olay günlüğüdür (append-only); MODULES.md güncel durumu yansıtır (aynı modülün bölümü değişiklikte güncellenir).




