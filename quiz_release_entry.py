"""Windows standalone entry point for the offline third-job quiz helper."""
import json
import os
from pathlib import Path
import sys
import tempfile

VERSION = '1.0.0'


def self_test(report_path):
    """Exercise packaged models, question data and native UI without the game."""
    report = dict(version=VERSION, frozen=bool(getattr(sys, 'frozen', False)))
    window = None
    try:
        with tempfile.TemporaryDirectory(prefix='maple-quiz-check-') as directory:
            os.environ['LOCALAPPDATA'] = directory
            import tkinter as tk
            import mss
            import numpy as np
            from PIL import Image, ImageDraw, ImageFont
            from game_capture import physical_dpi
            from quiz_bank import QuizEntry, load_bank, match_question
            from quiz_helper import QuizOCR, QuizWindow

            physical_dpi()
            bank = load_bank()
            assert len(bank) == 39, 'Unexpected question bank size'
            assert (Path(__file__).parent/'assets/maple-exp-icon-v1.ico').is_file()
            for entry in bank:
                match = match_question(entry.question+'\n'+entry.answer, confidence=1, entries=bank)
                assert match.entry is not None and match.entry.id == entry.id, entry.id
            report['bank_entries'] = len(bank)
            image = Image.new('RGB', (1040, 280), 'white')
            font = ImageFont.truetype(str(Path(os.environ['WINDIR'])/'Fonts/msyh.ttc'), 30)
            ImageDraw.Draw(image).multiline_text((24, 20),
                '以下哪个地方不是射手村的地图？\n1. 废弃都市\n2. 射手村公园\n3. 蘑菇公园',
                fill='#202020', font=font, spacing=18)
            engine = QuizOCR()
            text, confidence = engine.recognize(np.asarray(image)[:, :, ::-1].copy())
            sample = QuizEntry('generated', '以下哪个地方不是射手村的地图？', '废弃都市')
            match = match_question(text, confidence=confidence, entries=(sample,))
            assert match.status == 'matched' and match.entry.answer == '废弃都市', (text, confidence)
            report['ocr'] = dict(status='passed', confidence=confidence, evidence='synthetic Chinese question')
            with mss.mss() as capture:
                assert len(capture.monitors) > 1
            root = tk.Tk()
            root.withdraw()
            window = QuizWindow(root)
            root.update_idletasks()
            window.search.set('射手村')
            window.search_bank()
            assert window.filtered
            window.matches.selection_set(0)
            window.show_manual()
            assert window.answer.get() == window.filtered[0].answer, 'Manual answer not displayed'
            assert not window.running and window.region is None
            report['ui_manual_lookup'] = 'passed'
            window.close()
            window.worker.join(timeout=2)
            assert not window.worker.is_alive()
            window = None
            assert not list(Path(directory).rglob('*.sqlite3'))
            report['history_database_created'] = False
            report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
    finally:
        if window is not None and not window.closed:
            window.close()
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report.get('status') == 'passed' else 1


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test-report':
        return self_test(sys.argv[2])
    from quiz_helper import main as run
    run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
