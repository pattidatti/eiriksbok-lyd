#!/usr/bin/env python3
"""
Lager fortellerstemmen til artikkelfilmene i Eiriksbok.

For hvert manus i Eiriksbok (`src/features/film/manus/<fag>/<emne>/<id>.json`):

1. Regner ut et fingeravtrykk av alt som påvirker lyden (stemme, tempo, pauser, replikkene).
   Finnes lyd med samme fingeravtrykk fra før, hoppes filmen over.
2. Leser hver replikk med Google Chirp 3 HD (Algieba), én forespørsel per replikk.
3. Kontrollerer hver replikk med Nasjonalbibliotekets norske Whisper. Avviker det Whisper hører
   fra manuset, lages replikken på nytt én gang. Avviker den fortsatt, beholdes det beste
   forsøket og replikken listes under `avvik` (så et menneske kan lytte).
4. Setter replikkene sammen til ÉN mp3 per film, med pausene filmen skal ha, og skriver en
   json med start og slutt for hver replikk. Filmspilleren følger tidene i json-en.

Sikring mot regning: `kvote.json` teller tegn sendt til Google per måned. Jobben stopper før
den passerer MAKS_TEGN_PER_MND (gratiskvoten for Chirp 3 HD er 1 million tegn i måneden).

Bruk:
    python lag_lyd.py --manus ../Eiriksbok/src/features/film/manus            # lag det som mangler
    python lag_lyd.py --manus ... --bare-telle                                  # bare tell, ingen nett
    python lag_lyd.py --manus ... --film historie/industriell-revolusjon/titanic --tving

Miljø: GOOGLE_TTS_KEY (påkrevd for å lage lyd), GOOGLE_TTS_REFERER (bare for lokal testing med
en nettleser-låst nøkkel).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROT = Path(__file__).resolve().parent
SITE = ROT / 'site'
KVOTEFIL = ROT / 'kvote.json'

# Alt som påvirker lyden står her. Endres noe, får alle filmer nytt fingeravtrykk og lages på nytt.
STEMME = 'nb-NO-Chirp3-HD-Algieba'
TEMPO = 1.1
PAUSE_REPLIKK = 0.35
PAUSE_SCENE = 1.1
FORMATVERSJON = 1

RATE = 24000
MP3_BITRATE = '40k'
MAKS_TEGN_PER_MND = 800_000
AVVIK_GRENSE = 0.12  # andel tegn Whisper hører annerledes enn manuset (CER)
WHISPER = 'NbAiLab/nb-whisper-medium'

TTS_URL = 'https://texttospeech.googleapis.com/v1/text:synthesize'


# ---------- manus ----------


def finn_manus(manusmappe: Path) -> list[tuple[str, dict]]:
    """(nøkkel, manus) der nøkkel er artikkelstien uten skråstrek først, f.eks. historie/x/titanic."""
    ut = []
    for fil in sorted(manusmappe.rglob('*.json')):
        nokkel = fil.relative_to(manusmappe).with_suffix('').as_posix()
        ut.append((nokkel, json.loads(fil.read_text(encoding='utf-8'))))
    return ut


def replikker(manus: dict) -> list[dict]:
    """Flat liste med det stemmen skal si, og pausen etter hver replikk."""
    ut = []
    for scene in manus['scener']:
        rs = scene['replikker']
        for i, r in enumerate(rs):
            ut.append(
                {
                    'tekst': r.get('uttale') or r['si'],
                    'pause': PAUSE_SCENE if i == len(rs) - 1 else PAUSE_REPLIKK,
                }
            )
    return ut


def fingeravtrykk(rs: list[dict]) -> str:
    grunnlag = {
        'versjon': FORMATVERSJON,
        'stemme': STEMME,
        'tempo': TEMPO,
        'bitrate': MP3_BITRATE,
        'replikker': [[r['tekst'], r['pause']] for r in rs],
    }
    return hashlib.sha256(json.dumps(grunnlag, ensure_ascii=False).encode()).hexdigest()[:16]


def eksisterende(nokkel: str) -> dict | None:
    fil = SITE / 'film' / f'{nokkel}.json'
    if not fil.exists():
        return None
    try:
        return json.loads(fil.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return None


# ---------- kvote ----------


def denne_mnd() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')


def les_kvote() -> dict:
    if KVOTEFIL.exists():
        return json.loads(KVOTEFIL.read_text(encoding='utf-8'))
    return {}


def bruk_kvote(tegn: int) -> None:
    k = les_kvote()
    k[denne_mnd()] = k.get(denne_mnd(), 0) + tegn
    KVOTEFIL.write_text(json.dumps(k, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def brukt_i_mnd() -> int:
    return les_kvote().get(denne_mnd(), 0)


# ---------- stemme ----------


def tts(tekst: str) -> bytes:
    """Rå 16-bit PCM, mono, 24 kHz."""
    nokkel = os.environ.get('GOOGLE_TTS_KEY', '').strip()
    if not nokkel:
        sys.exit('GOOGLE_TTS_KEY mangler.')
    kropp = json.dumps(
        {
            'input': {'text': tekst},
            'voice': {'languageCode': 'nb-NO', 'name': STEMME},
            'audioConfig': {
                'audioEncoding': 'LINEAR16',
                'sampleRateHertz': RATE,
                'speakingRate': TEMPO,
            },
        }
    ).encode()
    hoder = {'Content-Type': 'application/json', 'x-goog-api-key': nokkel}
    if os.environ.get('GOOGLE_TTS_REFERER'):
        hoder['Referer'] = os.environ['GOOGLE_TTS_REFERER']
    for forsok in range(4):
        try:
            req = urllib.request.Request(TTS_URL, data=kropp, headers=hoder)
            with urllib.request.urlopen(req, timeout=60) as svar:
                data = json.loads(svar.read())
            break
        except urllib.error.HTTPError as e:
            melding = e.read().decode(errors='replace')[:400]
            if e.code in (429, 500, 503) and forsok < 3:
                time.sleep(5 * (forsok + 1))
                continue
            sys.exit(f'Google TTS svarte {e.code}: {melding}')
        except urllib.error.URLError:
            if forsok < 3:
                time.sleep(5 * (forsok + 1))
                continue
            raise
    bruk_kvote(len(tekst))
    wav = base64.b64decode(data['audioContent'])
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getframerate() == RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


def trim(pcm: bytes) -> bytes:
    """Kutter stillhet før og etter talen, med litt luft igjen, så tidene blir presise."""
    import numpy as np

    a = np.frombuffer(pcm, dtype=np.int16)
    over = np.nonzero(np.abs(a) > 400)[0]
    if len(over) == 0:
        return pcm
    luft = int(0.04 * RATE)
    return a[max(0, over[0] - luft) : min(len(a), over[-1] + luft)].tobytes()


# ---------- kontroll ----------

_whisper = None


def whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download

        sti = snapshot_download(WHISPER, allow_patterns=['ct2/*'])
        _whisper = WhisperModel(f'{sti}/ct2', device='cpu', compute_type='int8')
    return _whisper


def normaliser(tekst: str) -> str:
    from num2words import num2words

    t = tekst.lower()
    t = re.sub(r'(?<=\d)[ .](?=\d{3}\b)', '', t)  # «1 500» og «1.500» -> 1500
    t = re.sub(r'\d+', lambda m: ' ' + num2words(int(m.group()), lang='no') + ' ', t)
    t = re.sub(r'[^a-zæøå0-9 ]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


def hor(pcm: bytes) -> str:
    import numpy as np

    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768
    # 24 kHz -> 16 kHz for Whisper.
    x = np.linspace(0, len(a) - 1, int(len(a) * 16000 / RATE))
    a16 = np.interp(x, np.arange(len(a)), a).astype(np.float32)
    segs, _ = whisper().transcribe(
        a16, language='no', beam_size=5, condition_on_previous_text=False
    )
    return ' '.join(s.text.strip() for s in segs)


def avvik(fasit: str, hort: str) -> float:
    import jiwer

    f, h = normaliser(fasit), normaliser(hort)
    if not f:
        return 0.0
    return float(jiwer.cer(f, h))


# ---------- film ----------


def lag_film(nokkel: str, manus: dict, sjekk: bool) -> dict:
    import numpy as np

    rs = replikker(manus)
    biter: list[bytes] = []
    tider = []
    funn = []
    t = 0.0
    for i, r in enumerate(rs):
        pcm = trim(tts(r['tekst']))
        if sjekk:
            hort = hor(pcm)
            feil = avvik(r['tekst'], hort)
            if feil > AVVIK_GRENSE:
                pcm2 = trim(tts(r['tekst']))
                hort2 = hor(pcm2)
                feil2 = avvik(r['tekst'], hort2)
                if feil2 < feil:
                    pcm, hort, feil = pcm2, hort2, feil2
            if feil > AVVIK_GRENSE:
                funn.append({'indeks': i, 'tekst': r['tekst'], 'hort': hort, 'avvik': round(feil, 3)})
        lengde = len(pcm) / 2 / RATE
        tider.append({'tekst': r['tekst'], 'start': round(t, 3), 'slutt': round(t + lengde, 3)})
        biter.append(pcm)
        stille = int(r['pause'] * RATE)
        biter.append(np.zeros(stille, dtype=np.int16).tobytes())
        t += lengde + stille / RATE
        print(f'  {i + 1}/{len(rs)}' + (f'  AVVIK {funn[-1]["avvik"]}' if funn and funn[-1]['indeks'] == i else ''), flush=True)

    # Fingeravtrykket i filnavnet lar nettleseren cache lyden for alltid: ny lyd får nytt navn.
    avtrykk = fingeravtrykk(rs)
    ut = SITE / 'film' / f'{nokkel}-{avtrykk}.mp3'
    ut.parent.mkdir(parents=True, exist_ok=True)
    kod_mp3(b''.join(biter), ut)
    navn = Path(nokkel).name
    for gammel in ut.parent.glob(f'{navn}-*.mp3'):
        if gammel != ut and re.fullmatch(rf'{re.escape(navn)}-[0-9a-f]{{16}}\.mp3', gammel.name):
            gammel.unlink()
    return {
        'versjon': FORMATVERSJON,
        'hash': avtrykk,
        'stemme': STEMME,
        'tempo': TEMPO,
        'lyd': ut.name,
        'varighet': round(t, 3),
        'laget': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
        'kontrollert': sjekk,
        'replikker': tider,
        'avvik': funn,
    }


def ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return 'ffmpeg'


def kod_mp3(pcm: bytes, ut: Path) -> None:
    subprocess.run(
        [
            ffmpeg(), '-loglevel', 'error', '-y',
            '-f', 's16le', '-ar', str(RATE), '-ac', '1', '-i', '-',
            '-c:a', 'libmp3lame', '-b:a', MP3_BITRATE, str(ut),
        ],
        input=pcm,
        check=True,
    )


def skriv_oversikt() -> None:
    filmer = {}
    for fil in sorted((SITE / 'film').rglob('*.json')):
        d = json.loads(fil.read_text(encoding='utf-8'))
        nokkel = fil.relative_to(SITE / 'film').with_suffix('').as_posix()
        filmer[nokkel] = {'varighet': d['varighet'], 'laget': d['laget'], 'avvik': len(d['avvik'])}
    (SITE / 'filmer.json').write_text(
        json.dumps({'filmer': filmer}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument('--manus', required=True, type=Path, help='Eiriksbok/src/features/film/manus')
    p.add_argument('--film', help='bare denne filmen, f.eks. historie/industriell-revolusjon/titanic')
    p.add_argument('--tving', action='store_true', help='lag på nytt selv om lyden er oppdatert')
    p.add_argument('--bare-telle', action='store_true', help='skriv antall filmer som mangler lyd')
    p.add_argument('--uten-sjekk', action='store_true', help='hopp over Whisper-kontrollen')
    a = p.parse_args()

    alle = finn_manus(a.manus)
    if a.film:
        alle = [(n, m) for n, m in alle if n == a.film]
        if not alle:
            sys.exit(f'Fant ikke manus for {a.film}')

    mangler = []
    for nokkel, manus in alle:
        gammel = eksisterende(nokkel)
        if a.tving or not gammel or gammel.get('hash') != fingeravtrykk(replikker(manus)):
            mangler.append((nokkel, manus))

    if a.bare_telle:
        for n, _ in mangler:
            print(f'mangler: {n}', file=sys.stderr)
        print(len(mangler))
        return

    if not mangler:
        print('All filmlyd er oppdatert.')
        return

    for nokkel, manus in mangler:
        tegn = sum(len(r['tekst']) for r in replikker(manus))
        # Plass til at hver replikk kan lages på nytt én gang i verste fall.
        if brukt_i_mnd() + tegn * 2 > MAKS_TEGN_PER_MND:
            print(
                f'STOPP: {brukt_i_mnd()} tegn brukt i {denne_mnd()}. {nokkel} ({tegn} tegn) '
                f'kunne passert grensen på {MAKS_TEGN_PER_MND}. Prøver igjen neste måned.'
            )
            break
        print(f'{nokkel}: {tegn} tegn', flush=True)
        data = lag_film(nokkel, manus, sjekk=not a.uten_sjekk)
        fil = SITE / 'film' / f'{nokkel}.json'
        fil.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f'  ferdig: {data["varighet"]:.0f} s, {len(data["avvik"])} avvik', flush=True)

    skriv_oversikt()


if __name__ == '__main__':
    main()
