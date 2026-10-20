import copy
from fractions import Fraction
import hashlib
import json
import shutil
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

CENTRAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CENTRAL/'ferramentas/premiere'))
from converter_hdr import hdr_matches, generated_sdr, prepare, validate_conversion, timing_options
from exportar_premiere import discover, VideoTimingError, presentation_signature


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class HDRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=CENTRAL/'testes')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.original = self.folder/'IMG_1.MOV'
        self.original.write_bytes(b'original HDR')
        self.metadata = self.folder/'metadados'
        self.metadata.mkdir()
        self.source = dict(path=str(self.original), name=self.original.name,
            sha256=file_hash(self.original), fps='30', duration=2, decoded_frames=60,
            channels=2, sample_rate=48000, width=1920, height=1080, rotation_degrees=-90,
            video_time_base='1/600', presentation_sha256='a'*64,
            pixel_format='yuv420p10le', color=dict(color_transfer='arib-std-b67',
                color_primaries='bt2020', color_space='bt2020nc'))
        self.preset = json.loads((CENTRAL/'config/hdr-sdr.json').read_text(encoding='utf-8'))

    def probe(self, path):
        result = copy.deepcopy(self.source)
        result.update(path=str(path), name=Path(path).name, sha256=file_hash(path),
                      width=1080, height=1920, rotation_degrees=0,
                      color={k: 'bt709' for k in self.source['color']})
        return result

    def fake_ffmpeg(self, args, *rest):
        if args[-1] != '-':
            Path(args[-1]).write_bytes(b'SDR convertido')

    def convert(self, runner=None):
        with patch('converter_hdr.select_encoder', return_value=self.preset['gpu_encoder']):
            return prepare(self.source, self.metadata, self.preset,
                           runner or self.fake_ffmpeg, self.probe, file_hash)

    def test_matching_is_restricted_to_approved_profile(self):
        self.assertTrue(hdr_matches(self.source))
        for key, value in [('color_transfer', 'smpte2084'), ('color_primaries', 'bt709')]:
            changed = copy.deepcopy(self.source)
            changed['color'][key] = value
            self.assertFalse(hdr_matches(changed))
        self.assertFalse(hdr_matches({**self.source, 'pixel_format': 'yuv420p'}))

    def test_encoder_uses_source_precision_not_rounded_fps(self):
        calls = []
        def capture(args, *rest):
            calls.append(args)
            self.fake_ffmpeg(args, *rest)
        converted = self.convert(capture)
        command = calls[0]
        self.assertEqual(command[command.index('-enc_time_base:v')+1], '1/600')
        self.assertEqual(command[command.index('-video_track_timescale')+1], '600')
        self.assertNotIn('-r', command)
        self.assertEqual(command[command.index('-vf')+1], self.preset['filter']+',format=p010le')
        self.assertTrue(converted['hdr_conversion']['presentation_verified'])
        self.assertIsNone(converted['hdr_conversion']['timing_recovery'])

    def test_iphone_rounding_collision_is_not_a_broken_original(self):
        # Dois PTS reais do IMG_4532: distintos em 1/600, iguais após arredondar para 29,97 fps.
        pts = [27017, 27037]
        coarse = [round(p*Fraction(1,600)*Fraction(30000,1001)) for p in pts]
        self.assertEqual(coarse, [1350,1350])
        self.assertNotEqual(pts[0], pts[1])
        self.assertEqual(timing_options(self.source)[3], '1/600')

    def test_presentation_hash_is_independent_of_container_time_base(self):
        self.assertEqual(presentation_signature([0,20,41], '1/600'),
                         presentation_signature([120,160,202], '1/1200'))
        self.assertNotEqual(presentation_signature([0,20,41], '1/600'),
                            presentation_signature([0,20,40], '1/600'))

    def test_timing_failure_retries_once_with_cpu_and_same_color(self):
        calls = []
        probes = []
        def fail_first(path):
            probes.append(path)
            if len(probes) == 1:
                raise VideoTimingError('PTS repetido')
            return self.probe(path)
        def capture(args, *rest):
            calls.append(args)
            self.fake_ffmpeg(args, *rest)
        with patch('converter_hdr.select_encoder', return_value=self.preset['gpu_encoder']):
            result = prepare(self.source, self.metadata, self.preset, capture, fail_first, file_hash)
        conversions = [c for c in calls if '-vf' in c]
        self.assertEqual(len(conversions), 2)
        self.assertEqual(conversions[1][conversions[1].index('-c:v')+1], 'libx265')
        self.assertEqual(conversions[0][conversions[0].index('-vf')+1], conversions[1][conversions[1].index('-vf')+1])
        self.assertEqual(result['hdr_conversion']['timing_recovery'], 'PTS repetido')
        self.assertTrue(result['hdr_conversion']['presentation_verified'])

    def test_repeated_timing_failure_never_publishes_or_ignores(self):
        with patch('converter_hdr.select_encoder', return_value=self.preset['gpu_encoder']):
            with self.assertRaisesRegex(VideoTimingError, 'nenhuma cópia insegura'):
                prepare(self.source, self.metadata, self.preset, self.fake_ffmpeg,
                        lambda path: (_ for _ in ()).throw(VideoTimingError('PTS repetido')), file_hash)
        self.assertFalse((self.folder/'sdr IMG_1.mp4').exists())
        self.assertFalse(list(self.metadata.glob('*.partial.mp4')))
        self.assertEqual(file_hash(self.original), self.source['sha256'])

    def test_temporal_change_without_duplicate_is_also_rejected(self):
        converted = self.probe(self.original)
        converted['presentation_sha256'] = 'b'*64
        with self.assertRaisesRegex(VideoTimingError, 'instantes exatos'):
            validate_conversion(self.source, converted, exact_timing=True)

    def test_non_timing_failure_does_not_trigger_reencoding(self):
        calls = []
        def fail(args, *rest):
            calls.append(args)
            raise RuntimeError('Sem espaço no disco')
        with self.assertRaisesRegex(RuntimeError, 'espaço'):
            self.convert(fail)
        self.assertEqual(len(calls), 1)

    def test_cpu_failure_does_not_retry_same_encoder_forever(self):
        calls = []
        def run(args, *rest):
            calls.append(args)
            self.fake_ffmpeg(args, *rest)
        with patch('converter_hdr.select_encoder', return_value=self.preset['cpu_encoder']):
            with self.assertRaises(VideoTimingError):
                prepare(self.source, self.metadata, self.preset, run,
                        lambda path: (_ for _ in ()).throw(VideoTimingError('PTS inválido')), file_hash)
        self.assertEqual(len(calls), 1)
        self.assertFalse(list(self.metadata.glob('*.partial.mp4')))

    def test_sdr_is_not_processed(self):
        source = {**self.source, 'color': {k: 'bt709' for k in self.source['color']}}
        self.assertIs(prepare(source, self.metadata, self.preset, None, None, None), source)

    def test_online_copies_existing_sdr_to_metadata_without_reencoding(self):
        original_sdr = self.folder/'sdr IMG_1.mp4'
        original_sdr.write_bytes(b'SDR anterior')
        with patch.dict('os.environ', {'PREEDITOR_METADATA_ROOT': str(self.metadata)}), \
             patch('converter_hdr.select_encoder', side_effect=AssertionError('Nao recodificar.')):
            result = prepare(self.source, self.metadata, self.preset, self.fake_ffmpeg, self.probe, file_hash)
        self.assertTrue(Path(result['path']).is_relative_to(self.metadata/'Midia'))
        self.assertEqual(Path(result['path']).read_bytes(), original_sdr.read_bytes())
        self.assertTrue(result['sdr_reused'])

    def test_conversion_and_cache_preserve_original(self):
        first = self.convert()
        self.assertEqual(Path(first['path']).name, 'sdr IMG_1.mp4')
        self.assertEqual(first['hdr_conversion']['original_source'], self.source)
        self.assertEqual(file_hash(self.original), self.source['sha256'])
        def forbidden(*args):
            self.fail('Cache não deveria recodificar.')
        self.assertEqual(self.convert(forbidden)['sha256'], first['sha256'])

    def test_modified_cache_output_is_not_overwritten(self):
        result = self.convert()
        target = Path(result['path'])
        target.write_bytes(b'edicao manual')
        with self.assertRaisesRegex(ValueError, 'alterada'):
            self.convert()
        self.assertEqual(target.read_bytes(), b'edicao manual')

    def test_untracked_destination_is_reused_without_conversion(self):
        target = self.folder/'sdr IMG_1.mp4'
        target.write_bytes(b'nao sobrescrever')
        def check_only(args, *rest):
            self.assertEqual(args[-3:], ['-f', 'null', '-'])
            self.assertNotIn('-vf', args)
            self.assertEqual(args[args.index('-i')+1], str(target))
        with patch('converter_hdr.select_encoder', side_effect=AssertionError('Não selecionar encoder.')):
            result = prepare(self.source, self.metadata, self.preset, check_only, self.probe, file_hash)
        self.assertTrue(result['sdr_reused'])
        self.assertFalse(result['hdr_conversion']['preset_verified'])
        self.assertEqual(target.read_bytes(), b'nao sobrescrever')
        self.assertEqual(len(list(self.metadata.glob('hdr-sdr-*.json'))), 1)
        self.assertTrue(self.convert(lambda *args: self.fail('Não conferir/reconverter cache válido.'))['sdr_reused'])

    def test_existing_invalid_sdr_is_not_replaced_or_converted(self):
        target = self.folder/'sdr IMG_1.mp4'
        target.write_bytes(b'arquivo existente invalido')
        def bad_probe(path):
            return {**self.probe(path), 'duration': 7}
        with self.assertRaisesRegex(ValueError, 'duração'), patch('converter_hdr.select_encoder', side_effect=AssertionError()):
            prepare(self.source, self.metadata, self.preset, None, bad_probe, file_hash)
        self.assertEqual(target.read_bytes(), b'arquivo existente invalido')
        self.assertFalse(list(self.metadata.glob('hdr-sdr-*.json')))

    def test_existing_corrupt_sdr_stays_intact_without_cache(self):
        target = self.folder/'sdr IMG_1.mp4'
        target.write_bytes(b'corrompido')
        def failure(*args):
            raise RuntimeError('decodificacao falhou')
        with self.assertRaisesRegex(RuntimeError, 'decodificacao'):
            self.convert(failure)
        self.assertEqual(target.read_bytes(), b'corrompido')
        self.assertFalse(list(self.metadata.glob('hdr-sdr-*.json')))

    def test_source_change_does_not_reuse_stale_sdr(self):
        first = self.convert()
        self.original.write_bytes(b'outro HDR com mesma duracao')
        self.source['sha256'] = file_hash(self.original)
        with self.assertRaisesRegex(ValueError, 'original mudou'):
            self.convert(lambda *args: self.fail('Não converter.'))
        self.assertEqual(file_hash(first['path']), first['sha256'])

    def test_moved_source_reuses_existing_conversion_record(self):
        first = self.convert()
        moved = self.folder/'outro local'
        moved.mkdir()
        self.original = self.original.rename(moved/self.original.name)
        Path(first['path']).rename(moved/Path(first['path']).name)
        self.source['path'] = str(self.original)
        reused = self.convert(lambda *args: self.fail('Não reconverter após mover.'))
        self.assertEqual(reused['hdr_conversion']['preset'], self.preset['preset'])
        self.assertTrue(reused['sdr_reused'])

    def test_sdr_hash_change_during_validation_is_rejected(self):
        target = self.folder/'sdr IMG_1.mp4'
        target.write_bytes(b'existente')
        def change(*args):
            target.write_bytes(b'modificado durante conferencia')
        with self.assertRaisesRegex(ValueError, 'mudou durante'):
            self.convert(change)
        self.assertFalse(list(self.metadata.glob('hdr-sdr-*.json')))

    def test_failed_conversion_removes_only_new_intermediate(self):
        def failure(args, *rest):
            Path(args[-1]).write_bytes(b'incompleto')
            raise RuntimeError('erro simulado')
        with self.assertRaises(RuntimeError):
            self.convert(failure)
        self.assertFalse(list(self.metadata.glob('*.partial.mp4')))
        self.assertFalse((self.folder/'sdr IMG_1.mp4').exists())
        self.assertEqual(file_hash(self.original), self.source['sha256'])

    def test_timing_and_resolution_are_validated(self):
        self.source['fps'] = '30000/1001'
        converted = self.probe(self.original)
        validate_conversion(self.source, converted)
        for key, value in [('duration', 3), ('decoded_frames', 59), ('width', 1920)]:
            with self.assertRaises(ValueError):
                validate_conversion(self.source, {**converted, key: value})

    def test_discovery_ignores_generated_sdr(self):
        edition = self.folder/'clientes/cliente/edicao'
        edition.mkdir(parents=True)
        for name in ['video 1.MOV', 'sdr video 1.mp4', 'SDR video 2.mp4']:
            (edition/name).write_bytes(b'teste')
        jobs = list(discover(self.folder/'clientes'))
        self.assertEqual([p.name for p in jobs[0][1]], ['video 1.MOV'])
        self.assertTrue(generated_sdr('SDR video.mp4'))
        self.assertFalse(generated_sdr('sdrama.mp4'))

    def test_real_short_hdr_conversion_without_api(self):
        from exportar_premiere import probe, ffmpeg_run
        original = self.folder/'iphone.mov'
        subprocess.run(['ffmpeg', '-hide_banner', '-v', 'error', '-n', '-f', 'lavfi',
            '-i', 'color=c=gray:s=256x256:r=30:d=0.5', '-f', 'lavfi',
            '-i', 'anullsrc=r=48000:cl=stereo', '-t', '0.5', '-c:v', 'libx265',
            '-pix_fmt', 'yuv420p10le', '-x265-params',
            'pools=2:log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc',
            '-color_primaries', 'bt2020', '-color_trc', 'arib-std-b67',
            '-colorspace', 'bt2020nc', '-c:a', 'aac', str(original)],
            check=True, capture_output=True)
        source = probe(original)
        self.assertTrue(hdr_matches(source), source)
        converted = prepare(source, self.metadata, self.preset, ffmpeg_run, probe, file_hash)
        self.assertEqual(converted['color']['color_transfer'], 'bt709')
        self.assertEqual(converted['hdr_conversion']['full_decode'], 'OK')
        self.assertEqual(file_hash(original), source['sha256'])
        # Simula remoção de metadados mantendo o MP4 já aprovado na pasta.
        for cache in self.metadata.glob('hdr-sdr-*.json'):
            cache.unlink()
        original_sdr_hash = file_hash(converted['path'])
        def check_only(args, *rest):
            self.assertEqual(args[-3:], ['-f', 'null', '-'])
            self.assertNotIn('-vf', args)
            return ffmpeg_run(args, *rest)
        with patch('converter_hdr.select_encoder', side_effect=AssertionError('Não recodificar.')):
            reused = prepare(source, self.metadata, self.preset, check_only, probe, file_hash)
        self.assertTrue(reused['sdr_reused'])
        self.assertEqual(file_hash(reused['path']), original_sdr_hash)
        # Mesmo teste real pelo encoder CPU: precisão e preset também precisam sobreviver.
        cpu_metadata = self.folder/'teste-cpu'
        cpu_metadata.mkdir()
        cpu_original = self.folder/'cpu-fixture.mov'
        shutil.copy2(original, cpu_original)
        cpu_source = probe(cpu_original)
        with patch.dict('os.environ', {'PREEDITOR_METADATA_ROOT': str(cpu_metadata)}), \
             patch('converter_hdr.select_encoder', return_value=self.preset['cpu_encoder']):
            cpu = prepare(cpu_source, cpu_metadata, self.preset, ffmpeg_run, probe, file_hash)
        self.assertEqual(cpu['presentation_sha256'], cpu_source['presentation_sha256'])
        self.assertEqual(cpu['hdr_conversion']['encoder'], self.preset['cpu_encoder'])


if __name__ == '__main__':
    unittest.main()
