"""Pico-alvo 0 dBFS, com no máximo +6 dB de amplificação acumulada."""
import math
from fractions import Fraction
import wave

MAX_GAIN_DB = 6.0
PEAK_TARGET_DBFS = 0.0


def limited_gain_db(peak_dbfs, previous_gain_db=0.0, target_dbfs=PEAK_TARGET_DBFS,
                    max_gain_db=MAX_GAIN_DB):
    values = (peak_dbfs, previous_gain_db, target_dbfs, max_gain_db)
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError('Normalização recebeu um valor não finito.')
    limit = min(MAX_GAIN_DB, float(max_gain_db))
    if limit < 0 or target_dbfs > 0:
        raise ValueError('Limite de ganho inválido ou pico-alvo acima de 0 dBFS.')
    return min(float(target_dbfs)-float(peak_dbfs), limit, limit-float(previous_gain_db))


def clip_audio_gains(source):
    """Mede cada trecho, em todos os canais; devolve ganho linear editável."""
    import numpy as np
    treated = source['treated_audio']
    with wave.open(treated['path'], 'rb') as wav:
        rate, channels = wav.getframerate(), wav.getnchannels()
        if wav.getsampwidth() != 3 or channels != source['channels']:
            raise ValueError('Normalização exige WAV tratado PCM de 24 bits e canais preservados.')
        raw = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.uint8).reshape(-1, 3)
        samples = raw[:, 0].astype(np.int32) | raw[:, 1].astype(np.int32)<<8 | raw[:, 2].astype(np.int32)<<16
        samples = ((samples ^ 0x800000)-0x800000).reshape(-1, channels)
    prior = float(treated.get('gain_db', 0.0))
    ceiling = min(MAX_GAIN_DB, float(treated.get('max_gain_db', MAX_GAIN_DB)))
    fps = Fraction(source['fps'])
    gains = []
    for begin, end in source['kept_frames']:
        left, right = round(Fraction(begin, 1)/fps*rate), round(Fraction(end, 1)/fps*rate)
        if not 0 <= left < right <= len(samples):
            raise ValueError('Trecho de normalização fora do WAV tratado.')
        peak = int(np.abs(samples[left:right]).max())
        if peak:
            peak_dbfs = 20*math.log10(peak/8388607)
            gain_db = limited_gain_db(peak_dbfs, prior, max_gain_db=ceiling)
        else:
            peak_dbfs = None
            gain_db = min(0.0, ceiling-prior)  # Silêncio não recebe boost.
        gains.append({'start_frame': begin, 'end_frame': end, 'peak_dbfs': peak_dbfs,
                      'gain_db': gain_db, 'linear': 10**(gain_db/20),
                      'wav_gain_db': prior, 'total_gain_db': prior+gain_db,
                      'peak_target_dbfs': PEAK_TARGET_DBFS, 'max_gain_db': ceiling})
    return gains
