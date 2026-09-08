#!/usr/bin/env python3
"""DENEY: çoklu-segment olay kalıbı — python_scripts/experiment_multiline_llm_recon.py

Amaç (A/B deneyi):
  call_llm_for_pattern_recon.py'nin BİREBİR kopyasıdır; tek fark promptun
  kendi config klasöründen (experiment_multiline_llm_recon_config/) gelmesi:
  sistem promptu ÇOKLU-SEGMENT olay kalıplarına izin verir (regex içinde
  " _EOL_ _SOL_ " ayracı + tekrarlayan orta kısımlar (...)+ ). Hedef: LLM'in
  stack trace gibi çok satırlı olayları TEK olay-kuralı olarak öğrenip
  öğrenemediğini kanıtlamak. Kanıtlanınca yeni prompt ana config'e taşınır
  ve bu deney script'i silinir.

  Kod, wire-log ve davranış kuralları ana script ile aynıdır (K1-K8); wire
  loglar stem olarak bu scriptin adını kullanır.

Örnek:
  python3 python_scripts/experiment_multiline_llm_recon.py \
    --input data/hadoop_ex_chunk_10k.txt \
    --output data/llm/experiment_multiline_patterns.json \
    --llm-dir data/llm \
    --report data/llm/report_experiment_multiline.json
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("experiment_multiline")

READ_CHUNK = 1 << 20            # 1 MB bloklarla oku (K2: tümünü belleğe yükleme)
RETRYABLE_HTTP = {429, 500, 502, 503, 504}  # bu kodlar yeniden denenebilir
DEFAULT_MODEL = "glm-5.3-flash"
# z.ai Coding Plan endpoint (ASLA DEĞİŞTİRME — kullanıcı aboneliği Coding Plan;
# genel API URL'i DEĞİL! Yanlış URL coding anahtarıyla 401 verir.)
DEFAULT_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_API_KEY_FILE = "/secrets/zai.env"   # compose: D:\secrets -> /secrets (ro)
MARKER_SOL = "_SOL_"            # girdi sözleşmesi: chunk.py çıktısı


class LLMHttpError(Exception):
    """Sunucu HTTP hata kodu döndü (gövde dahil)."""
    def __init__(self, status, body):
        super().__init__("HTTP %d" % status)
        self.status = status
        self.body = body


class LLMNetworkError(Exception):
    """Ağa/bağlantıya dair hata (DNS, timeout, bağlantı kesildi...)."""
    pass


class LLMStreamInterrupted(Exception):
    """Akış ilerleme kaydettikten SONRA kesildi; kısmi içerik taşınır.
    Otomatik retry yapılmaz — ilerleme (düşünme/cevap) kaybolmasın."""
    def __init__(self, assembled: dict, stats: dict, cause):
        super().__init__(str(cause))
        self.assembled = assembled
        self.stats = stats


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(path: str, payload: dict) -> None:
    """report.json'u atomik yazar: geçici dosyaya yaz + os.replace (K8)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def write_wire(path: str, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def read_env_file(path: str) -> dict:
    """KEY=VALUE satırlarını okur (# yorum ve boş satırları atlar)."""
    env = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def mask_key(key: str) -> str:
    return ("***" + key[-4:]) if len(key) > 8 else "***"


def resolve_api_key(args) -> tuple:
    """Anahtar çözüm sırası: --api-key > --api-key-file > ZAI_API_KEY env."""
    if args.api_key:
        return args.api_key, "--api-key parametresi"
    if os.path.isfile(args.api_key_file):
        env = read_env_file(args.api_key_file)
        if env.get("ZAI_API_KEY"):
            return env["ZAI_API_KEY"], args.api_key_file
        raise ValueError("API anahtar dosyası var ama içinde ZAI_API_KEY yok: %s"
                         % args.api_key_file)
    if os.environ.get("ZAI_API_KEY"):
        return os.environ["ZAI_API_KEY"], "ortam değişkeni ZAI_API_KEY"
    raise ValueError("API anahtarı bulunamadı — şunlardan biri gerekli: "
                     "--api-key, --api-key-file (varsayılan: %s) veya "
                     "ZAI_API_KEY ortam değişkeni" % args.api_key_file)


def read_input_text(path: str) -> str:
    """Girdiyi blok blok okuyup tek metne birleştirir (chunk çıktısı tek satırdır,
    boyut chunk bütçesiyle sınırlı olduğu için bellek güvenli kalır)."""
    parts = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        while True:
            block = f.read(READ_CHUNK)
            if not block:
                break
            parts.append(block)
    return "".join(parts).strip()


def next_wire_number(llm_dir: str, stem: str) -> int:
    """Mevcut en büyük NNNN'den devam eder (K6: üzerine yazma yok, restart'a dayanıklı)."""
    os.makedirs(llm_dir, exist_ok=True)
    n = 0
    for name in os.listdir(llm_dir):
        if name.startswith(stem + "_"):
            base = name[len(stem) + 1:].split("_", 1)[0]
            if base.isdigit():
                n = max(n, int(base))
    return n + 1


# CJK/Çince karakter taraması: ideograflar, ext-A, uyumluluk, kana, CJK noktalama,
# tamgeniş (fullwidth) formlar — cevapta hiçbiri olmamalı (yalnızca İngilizce).
CJK_RE = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf"
    "\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def extract_json_text(text: str) -> str:
    """Markdown çitlerini (```json ... ```) kabuktan soyar — yalnızca doğrulama
    için; çıktı dosyasına cevap OLDUĞU GİBİ yazılır, buna dokunulmaz."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def count_patterns(text: str) -> int:
    try:
        data = json.loads(extract_json_text(text))
        return len(data.get("patterns", []))
    except (ValueError, AttributeError):
        return 0


def validate_patterns_json(text: str):
    """Cevabın çıkış sözleşmesine uyduğunu denetler.
    None = uygun; aksi halde insan-okur ihlal sebebi döner."""
    if CJK_RE.search(text):
        return "Çince/CJK karakter var (yalnızca İngilizce kabul edilir)"
    try:
        data = json.loads(extract_json_text(text))
    except ValueError as exc:
        return "JSON parse edilemedi (%s)" % exc
    if not isinstance(data, dict) or not isinstance(data.get("patterns"), list):
        return "üst düzey şema hatalı: patterns listesi yok"
    if not data["patterns"]:
        return "patterns listesi boş"
    for i, pat in enumerate(data["patterns"]):
        if not isinstance(pat, dict):
            return "patterns[%d] nesne değil" % i
        for field in ("tag", "label", "readable", "regex"):
            if not pat.get(field):
                return "patterns[%d]: zorunlu alan '%s' yok/boş" % (i, field)
    return None


def http_post_json(url: str, body: dict, api_key: str, timeout: float) -> tuple:
    """POST atar; (http_status, parsed_json_or_None, raw_text, elapsed) döner.
    HTTP hatalarında LLMHttpError, ağ hatalarında LLMNetworkError fırlatır."""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": "Bearer " + api_key,
        })
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise LLMHttpError(exc.code, raw)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise LLMNetworkError(str(reason))
    elapsed = time.monotonic() - started
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    return status, parsed, raw, elapsed


def http_post_json_stream(url: str, body: dict, api_key: str, idle_timeout: float,
                          progress_every: float = 2.0, live_path: str = None) -> tuple:
    """POST atar (stream=true, SSE); delta'lar geldikçe stderr'e CANLI ilerleme
    yazar (K4+K7: akıl yürütme/cevap karakterleri anlık görünür).
    (http_status, birlestirilmis_cevap_dict, istatistik_dict, elapsed) döner.
    timeout = veri AKIŞI durursa iptal süresi (inactivity); delta'lar aktıkça
    koşu istediği kadar sürebilir."""
    body = dict(body)
    body["stream"] = True
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + api_key,
        })
    started = time.monotonic()
    content_parts = []
    reasoning_parts = []
    usage = {}
    finish_reason = None
    chunks = 0
    last_report = started
    try:
        with urllib.request.urlopen(req, timeout=idle_timeout) as resp:
            status = resp.status
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                chunks += 1
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") or {}) if choices else {}
                r_part = delta.get("reasoning_content") or ""
                c_part = delta.get("content") or ""
                if r_part:
                    reasoning_parts.append(r_part)
                if c_part:
                    content_parts.append(c_part)
                if choices and choices[0].get("finish_reason"):
                    finish_reason = choices[0]["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
                now = time.monotonic()
                if now - last_report >= progress_every:
                    log.info("akan: düşünme %d karakter, cevap %d karakter (%.0f sn)",
                             sum(len(p) for p in reasoning_parts),
                             sum(len(p) for p in content_parts),
                             now - started)
                    last_report = now
                    if live_path:  # GUI kalp atışı (atomik)
                        try:
                            tmp = live_path + ".tmp"
                            with open(tmp, "w", encoding="utf-8") as lf:
                                json.dump({"reasoning_chars": sum(len(p) for p in reasoning_parts),
                                           "content_chars": sum(len(p) for p in content_parts),
                                           "sse_chunks": chunks,
                                           "elapsed_sec": round(now - started, 1),
                                           "ts": time.time()}, lf)
                            os.replace(tmp, live_path)
                        except OSError:
                            pass
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise LLMHttpError(exc.code, raw)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if chunks > 0:
            # ilerleme vardı: retry edip düşünmeyi sıfırlamak yerine kısmiyi koru
            log.warning("akış ilerleme sonrası kesildi (%s): %d parça korundu, "
                        "otomatik yeniden deneme YAPILMAZ", reason, chunks)
            partial = {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "".join(content_parts),
                        "reasoning_content": "".join(reasoning_parts),
                    },
                    "finish_reason": finish_reason or "stream_interrupted",
                }],
                "usage": usage,
                "partial": True,
            }
            raise LLMStreamInterrupted(
                partial, {"sse_chunks": chunks,
                          "reasoning_chars": sum(len(p) for p in reasoning_parts)},
                reason)
        raise LLMNetworkError(str(reason))
    elapsed = time.monotonic() - started
    if reasoning_parts or content_parts:
        log.info("akış bitti: düşünme %d karakter, cevap %d karakter, %d SSE parçası, %.1f sn",
                 sum(len(p) for p in reasoning_parts),
                 sum(len(p) for p in content_parts), chunks, elapsed)
    assembled = {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "".join(content_parts),
                "reasoning_content": "".join(reasoning_parts),
            },
            "finish_reason": finish_reason,
        }],
        "usage": usage,
    }
    stats = {"sse_chunks": chunks,
             "reasoning_chars": sum(len(p) for p in reasoning_parts)}
    return status, assembled, stats, elapsed


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    stem = os.path.splitext(os.path.basename(__file__))[0]

    payload = {
        "script": stem + ".py",
        "status": "running",
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.output),
        "llm_dir": os.path.abspath(args.llm_dir),
        "model": args.model,          # None ise çözülünce dolar
        "base_url": args.base_url,
        "thinking": args.thinking,
        "temperature": args.temperature,
        "timeout_sec": args.timeout,
        "retries": args.retries,
        "started_at_utc": _utcnow(),
        "updated_at_utc": _utcnow(),
        "elapsed_sec": 0.0,
        "llm_calls": 0,
        "attempts_used": 0,
        "usage": {},
        "response_chars": 0,
    }

    def flush(status: str) -> None:
        payload["status"] = status
        payload["updated_at_utc"] = _utcnow()
        payload["elapsed_sec"] = round(time.monotonic() - started, 3)
        try:
            write_report(args.report, payload)
        except OSError as exc:
            log.warning("report.json yazılamadı: %s", exc)

    try:
        # --- 1) girdi sözleşmesi: chunk.py çıktısı olmalı -------------------
        chunk_text = read_input_text(args.input)
        if not chunk_text:
            raise ValueError("girdi boş: %s" % args.input)
        first_word = chunk_text.split(None, 1)[0]
        if first_word != MARKER_SOL:
            raise ValueError("girdi chunk.py çıktısı gibi görünmüyor (ilk kelime %r, "
                             "beklenen %r) — önce chunk.py çalıştırın"
                             % (first_word, MARKER_SOL))
        if len(chunk_text) > args.max_input_chars:
            raise ValueError("girdi %d karakter (--max-input-chars limiti %d) — "
                             "daha küçük bir chunk kullanın"
                             % (len(chunk_text), args.max_input_chars))
        log.info("girdi okundu: %d karakter", len(chunk_text))

        # --- 2) prompt dosyaları config klasöründen -------------------------
        sys_path = os.path.join(args.config_dir, args.system_prompt_file)
        usr_path = os.path.join(args.config_dir, args.user_prompt_file)
        with open(sys_path, "r", encoding="utf-8") as f:
            system_text = f.read()
        with open(usr_path, "r", encoding="utf-8") as f:
            user_template = f.read()
        if args.placeholder not in user_template:
            raise ValueError("user prompt'ta placeholder %r yok (%s) — şablona ekleyin"
                             % (args.placeholder, usr_path))
        user_text = user_template.replace(args.placeholder, chunk_text)
        log.info("promptlar yüklendi: system %d karakter, user %d karakter (chunk yerleşti)",
                 len(system_text), len(user_text))
        log.debug("config: %s | system: %s | user: %s", args.config_dir,
                  args.system_prompt_file, args.user_prompt_file)

        # --- 3) anahtar + model + url çözümü ---------------------------------
        api_key, key_source = resolve_api_key(args)
        env_file = read_env_file(args.api_key_file) if os.path.isfile(args.api_key_file) else {}
        model = (args.model or env_file.get("ZAI_MODEL")
                 or os.environ.get("ZAI_MODEL") or DEFAULT_MODEL)
        base_url = (args.base_url or env_file.get("ZAI_BASE_URL")
                    or os.environ.get("ZAI_BASE_URL") or DEFAULT_BASE_URL)
        url = base_url.rstrip("/") + "/chat/completions"
        payload["model"], payload["base_url"] = model, base_url
        log.debug("anahtar kaynağı: %s (%s) — anahtar loglara yazılmaz",
                  key_source, mask_key(api_key))

        messages = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]
        body = {
            "model": model,
            "messages": messages,
            "temperature": args.temperature,
            "thinking": {"type": args.thinking},
        }
        if args.response_format == "json":
            # API düzeyinde JSON modu: sunucu cevabı JSON olmaya zorlar
            body["response_format"] = {"type": "json_object"}
        if args.max_output_tokens > 0:
            body["max_tokens"] = args.max_output_tokens
        stream_mode = not args.no_stream
        if stream_mode:
            body["stream"] = True  # SSE: delta'lar canlı akar (K7)
        body_bytes = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
        log.info("LLM çağrısı hazir: %s | model=%s | istem %d bayt | json_mod=%s | stream=%s",
                 url, model, body_bytes, args.response_format == "json", stream_mode)

        # --- 4) deneme döngüsü: her deneme = bir wire-log çifti (K6) --------
        llm_dir = os.path.abspath(args.llm_dir)
        max_attempts = args.retries + 1
        parsed = None
        last_error = ""
        wire_number = 0
        final_content = None       # sözleşmeye uyan son cevap (olduğu gibi yazılır)
        contract_violation = ""    # son cevabın ihlal sebebi ("" = uygun)
        format_retries_used = 0

        for attempt in range(1, max_attempts + 1):
            payload["attempts_used"] = attempt
            wire_number = next_wire_number(llm_dir, stem)
            req_path = os.path.join(llm_dir, "%s_%04d_request.json" % (stem, wire_number))
            resp_path = os.path.join(llm_dir, "%s_%04d_response.json" % (stem, wire_number))
            payload["llm_calls"] += 1
            body["messages"] = messages  # format-düzeltme turunda mesajlar büyür
            write_wire(req_path, {
                "meta": {
                    "script": stem + ".py",
                    "attempt": attempt, "max_attempts": max_attempts,
                    "url": url, "model": model,
                    "api_key": mask_key(api_key), "key_source": key_source,
                    "started_at_utc": _utcnow(),
                },
                "body": body,
            })

            failure = None
            try:
                if stream_mode:
                    status, parsed, sstats, elapsed = http_post_json_stream(
                        url, body, api_key, args.stream_idle_timeout,
                        live_path=os.path.join(llm_dir, "live.json"))
                else:
                    status, parsed, raw, elapsed = http_post_json(
                        url, body, api_key, args.timeout)
                has_choice = bool(parsed and isinstance(parsed.get("choices"), list)
                                  and parsed["choices"])
                write_wire(resp_path, {
                    "meta": {
                        "attempt": attempt, "http_status": status,
                        "elapsed_sec": round(elapsed, 3), "ok": has_choice,
                        "stream": stream_mode,
                        **({"sse_chunks": sstats["sse_chunks"],
                            "reasoning_chars": sstats["reasoning_chars"]}
                           if stream_mode else {}),
                    },
                    "body": parsed if parsed is not None else {"raw": raw[:50000]},
                })
                if not has_choice:
                    failure = "beklenmedik yanıt biçimi (choices yok)"
                else:
                    content = parsed["choices"][0].get("message", {}).get("content") or ""
                    violation = validate_patterns_json(content)
                    if violation is None:
                        final_content = content
                        contract_violation = ""
                        log.info("deneme %d başarili: HTTP %d, %.1f sn, sözleşme OK (%d kalıp)",
                                 attempt, status, elapsed, count_patterns(content))
                    elif format_retries_used < args.format_retries:
                        format_retries_used += 1
                        log.warning("yanıt sözleşmeye uymuyor (%s) — düzeltme isteniyor (%d/%d)",
                                    violation, format_retries_used, args.format_retries)
                        messages = messages + [
                            {"role": "assistant", "content": content},
                            {"role": "user", "content":
                                "Your previous reply violated the output contract: %s. "
                                "Return ONLY the corrected JSON object now. English only, "
                                "no Chinese/CJK characters anywhere, no markdown fences, "
                                "no prose. It must parse with json.loads() and follow "
                                "the schema." % violation},
                        ]
                        failure = "format-geçersiz: %s" % violation
                    else:
                        final_content = content
                        contract_violation = violation
                        log.error("yanıt sözleşmeye uymuyor ve düzeltme hakkı bitti: %s", violation)
            except LLMHttpError as exc:
                write_wire(resp_path, {
                    "meta": {
                        "attempt": attempt, "http_status": exc.status,
                        "ok": False, "error": "HTTP %d" % exc.status,
                    },
                    "body_raw": exc.body[:50000],
                })
                failure = "HTTP %d: %s" % (exc.status, exc.body[:200])
            except LLMStreamInterrupted as exc:
                # ilerleme vardı: retry düşünmeyi sıfırlardı — kısmiyi koru, dürüst düş
                write_wire(resp_path, {
                    "meta": {
                        "attempt": attempt, "http_status": None, "ok": False,
                        "error": "stream_interrupted: %s" % exc, "stream": True,
                        "partial": True, **exc.stats,
                    },
                    "body": exc.assembled,
                })
                raise RuntimeError(
                    "LLM akışı ilerleme kaydedildikten sonra kesildi "
                    "(%d SSE parçası, %d düşünme karakteri korundu — wire logda); "
                    "otomatik yeniden deneme YAPILMAZ, elle yeniden koşun"
                    % (exc.stats["sse_chunks"], exc.stats["reasoning_chars"]))
            except LLMNetworkError as exc:
                write_wire(resp_path, {
                    "meta": {
                        "attempt": attempt, "http_status": None,
                        "ok": False, "error": "network: %s" % exc,
                    },
                })
                failure = "ağ hatası: %s" % exc

            if failure is None:
                break

            last_error = failure
            is_net = failure.startswith("ağ hatası:")
            is_bad_format = (failure.startswith("beklenmedik yanıt biçimi")
                             or failure.startswith("format-geçersiz"))
            is_retryable_http = (failure.startswith("HTTP ")
                                 and int(failure.split()[1].rstrip(":")) in RETRYABLE_HTTP)
            if not (is_net or is_bad_format or is_retryable_http):
                # 401/403/400 gibi kalıcı hatalar: tekrar denemek boşa, hemen düş
                raise RuntimeError("LLM çağrısı başarısız (yeniden denenebilir değil): %s"
                                   % last_error)
            if attempt < max_attempts:
                delay = min(args.retry_backoff * (2 ** (attempt - 1)), args.retry_backoff_max)
                log.warning("deneme %d/%d başarisiz (%s) — %.1f sn sonra tekrar",
                            attempt, max_attempts, failure, delay)
                flush("running")
                time.sleep(delay)
            else:
                raise RuntimeError("LLM çağrısı %d denemede başarisiz: %s"
                                   % (max_attempts, last_error))
            flush("running")

        # --- 5) cevabın metnini çıktıya yaz (OLDUĞU GİBİ — dokunulmaz) -------
        content = final_content or ""
        out_dir = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(out_dir, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(content)
            if content and not content.endswith("\n"):
                f.write("\n")

        payload.update({
            "usage": parsed.get("usage", {}) if parsed else {},
            "response_chars": len(content),
            "wire_last_number": wire_number,
            "json_valid": not contract_violation,
            "contract_violation": contract_violation or None,
            "format_retries_used": format_retries_used,
            "pattern_count": count_patterns(content),
        })
        if not contract_violation:
            flush("completed")
            log.info("bitti: cevap %d karakter, %d kalıp -> %s | çağrı %d (deneme %d) | "
                     "wire #%04d | %.1f sn",
                     len(content), payload["pattern_count"], args.output,
                     payload["llm_calls"], payload["attempts_used"], wire_number,
                     payload["elapsed_sec"])
            log.debug("usage: %s", json.dumps(payload["usage"], ensure_ascii=False))
            return 0
        # cevap geldi ama sözleşmeye uymuyor: ham metin yazıldı, pipeline durmalı (K3)
        payload["error"] = "LLM cevabı JSON sözleşmesine uymuyor: %s" % contract_violation
        flush("invalid_response")
        log.error("hata: %s (ham cevap yine de %s dosyasında)", payload["error"], args.output)
        return 1
    except KeyboardInterrupt:
        flush("interrupted")
        log.error("kullanıcı tarafından kesildi")
        return 130
    except Exception as exc:  # traceback yok, temiz mesaj, sıfır dışı çıkış (K3)
        payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        flush("failed")
        log.error("hata: %s", payload["error"])
        return 1


def nonnegative_int(value: str) -> int:
    try:
        iv = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("tam sayı girin: %r" % value)
    if iv < 0:
        raise argparse.ArgumentTypeError("negatif olamaz: %r" % value)
    return iv


def parse_args(argv=None) -> argparse.Namespace:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    default_config_dir = os.path.join(
        script_dir, os.path.splitext(os.path.basename(__file__))[0] + "_config")

    p = argparse.ArgumentParser(
        prog="experiment_multiline_llm_recon.py",
        description="DENEY: çoklu-segment olay kalıbı çıkarımı (A/B — ana scriptin "
                    "kopyası, farklı prompt config'i). Promptlar <config-dir> altındaki "
                    "dosyalardan okunur. Her çağrı/yanıt --llm-dir altına JSON yazılır (K6).")
    p.add_argument("--input", required=True,
                   help="girdi dosyası (chunk.py çıktısı, _SOL_ ile başlar)")
    p.add_argument("--output", required=True,
                   help="çıktı dosyası: LLM cevabının metin kısmı")
    p.add_argument("--config-dir", default=default_config_dir,
                   help="prompt klasörü (varsayılan: script yanı <adı>_config)")
    p.add_argument("--system-prompt-file", default="system_prompt.txt",
                   help="config klasöründeki sistem mesajı dosyası")
    p.add_argument("--user-prompt-file", default="user_prompt.txt",
                   help="config klasöründeki kullanıcı mesajı şablonu")
    p.add_argument("--placeholder", default="$hadoop",
                   help="user prompttaki yer tutucu; girdi içeriğiyle değiştirilir")
    p.add_argument("--model", default=None,
                   help="model adı (varsayılan: ZAI_MODEL dosya/ortam, yoksa %s)" % DEFAULT_MODEL)
    p.add_argument("--base-url", default=None,
                   help="API base URL (varsayılan: ZAI_BASE_URL dosya/ortam, yoksa %s)"
                    % DEFAULT_BASE_URL)
    p.add_argument("--api-key", default=None,
                   help="API anahtarı (önerilmez; --api-key-file veya ortam değişkeni tercih edin)")
    p.add_argument("--api-key-file", default=DEFAULT_API_KEY_FILE,
                   help="KEY=VALUE biçimli anahtar dosyası (varsayılan: %s)" % DEFAULT_API_KEY_FILE)
    p.add_argument("--thinking", choices=["enabled", "disabled"], default="enabled",
                   help="gelişmiş akıl yürütme modu (varsayılan: enabled)")
    p.add_argument("--response-format", choices=["json", "text"], default="json",
                   help="json: API düzeyinde JSON modu (response_format=json_object), "
                        "cevap sunucu tarafında JSON olmaya zorlanır (varsayılan: json)")
    p.add_argument("--format-retries", type=nonnegative_int, default=1,
                   help="yanıt JSON/CJK sözleşmesine uymazsa düzeltme mesajıyla "
                        "yeniden isteme hakkı (varsayılan: 1)")
    p.add_argument("--no-stream", action="store_true",
                   help="SSE akışını kapat (varsayılan: akış AÇIK — düşünme/cevap "
                        "karakterleri canlı akar; timeout, veri akışı durursa geçerlidir)")
    p.add_argument("--temperature", type=float, default=0.1,
                   help="örnekleme sıcaklığı (varsayılan: 0.1 — determinizme yakın)")
    p.add_argument("--max-output-tokens", type=nonnegative_int, default=0,
                   help="yanıt token sınırı (0 = sunucu varsayılanı)")
    p.add_argument("--max-input-chars", type=int, default=2000000,
                   help="girdi karakter sınırı, koruma (varsayılan: 2000000)")
    p.add_argument("--timeout", type=float, default=120.0,
                   help="stream kapalıyken ağ timeout'u, saniye (varsayılan: 120)")
    p.add_argument("--stream-idle-timeout", type=float, default=300.0,
                   help="stream açıkken veri TAMAMEN bu kadar saniye susarsa iptal "
                        "(varsayılan: 300). Delta'lar aktıkça —düşünme/cevap arttıkça— "
                        "koşu istediği kadar sürer, kesilmez")
    p.add_argument("--retries", type=nonnegative_int, default=2,
                   help="ek yeniden deneme sayısı (varsayılan: 2 → toplam 3 deneme)")
    p.add_argument("--retry-backoff", type=float, default=2.0,
                   help="yeniden deneme bekleme tabanı, saniye; üstel artar (varsayılan: 2.0)")
    p.add_argument("--retry-backoff-max", type=float, default=30.0,
                   help="yeniden deneme bekleme tavanı, saniye (varsayılan: 30.0)")
    p.add_argument("--llm-dir", default="llm",
                   help="wire log klasörü (varsayılan: ./llm)")
    p.add_argument("--report", default="report.json",
                   help="çalışma raporu dosyası (varsayılan: report.json)")
    p.add_argument("--debug", action="store_true", help="ayrıntılı log (stderr)")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,  # K4: loglar stderr'de, stdout temiz
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(run(args))


if __name__ == "__main__":
    main()
