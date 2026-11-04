"""エージェントが読む文書から、解き方の文が消えないことを確かめる。"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / 'tests/doc-baseline.json').read_text())
SKILLS = 'template/.agents/skills/'

# 消すと解き方が変わる文。言い換えるなら、ここも理由を確かめて直す。
PHRASES = {
    'template/AGENTS.md': [
        '「十分」「危険」「大変」で止まらない', '止まるのはマニュアルの禁止に当たるときだけ',
        '捨てるもの', '一般的なベストプラクティス', '無くせないかを考える', '施策の大きさで選ぶ',
        '何倍になるか', '視点を変える', '別のチームから', '前提から', '0 から作るならこの形を選ぶか',
        '期待値で決める',
    ],
    'docs/strategy.md': [
        '却下の理由にしない', 'スコアの目標を持たず', '傾向は実験で掴む', '構成は紙の上の計算で決める',
        '作り直しは今の版を止めずにブランチで進め',
    ],
    SKILLS + 'main-loop/SKILL.md': [
        'やめる理由にならない', 'pass する確率 × 点', '平均で「余っている」と読まない',
        '要求と、直前に返した応答を並べる', '先に作っている量を数える',
        '「スコアが伸びない」', '「何から手をつける？」',
    ],
    SKILLS + 'experiment-rules/SKILL.md': ['二峰は早く疑う', '止まらない', '「この変更は効いた？」'],
    SKILLS + 'delegate/SKILL.md': ['依頼文の雛形', '要約は詳細が欠ける'],
    SKILLS + 'isu-analyze/SKILL.md': ['idle が多いのにスコアが低い', 'Calls が桁違いに多い'],
    SKILLS + 'isu-finalize/SKILL.md': ['起動順序に依存していないか', '前倒ししない'],
    SKILLS + 'isu-kickoff/SKILL.md': ['壊しても戻せる床', 'スクリプトを疑う前に環境差分を疑う'],
    'tools/checkpoint/checkpoint': ['前もって計算できるか', '0 ベースの設計の骨格'],
    'tools/checkpoint/audit/SKILL.md': ['待機している・返事を待っている', '却下した案の理由が、ボトルネックを取り除けるかの計算でない'],
}

# template/ に置かない語（キットの決まり）
FORBIDDEN = re.compile(r'ISUCON\s?\d|isucon\d|isuride|地域|最終日|ISUReal')


class DocumentTests(unittest.TestCase):
    def test_solving_phrases_remain(self):
        for rel, phrases in PHRASES.items():
            text = (ROOT / rel).read_text()
            for phrase in phrases:
                self.assertIn(phrase, text, f'{rel} から「{phrase}」が消えた。消す理由を確かめる')

    def test_solving_comes_before_prohibitions(self):
        text = (ROOT / 'template/AGENTS.md').read_text()
        self.assertLess(text.index('## 考え方'), text.index('## 競技ルールで絶対に踏まないもの'))
        self.assertLess(text.index('## 価値観'), text.index('## 競技ルールで絶対に踏まないもの'))
        self.assertLess(text.index('## 道具'), text.index('## 競技ルールで絶対に踏まないもの'))

    def test_documents_do_not_shrink_silently(self):
        for rel, size in BASELINE.items():
            now = len((ROOT / rel).read_text())
            self.assertGreaterEqual(now, size * 0.8,
                                    f'{rel} が {size} 字から {now} 字に減った。消す文と理由を確かめ、tests/doc-baseline.json を更新する')

    def test_no_problem_specific_words_in_template(self):
        for path in (ROOT / 'template').rglob('*'):
            if not path.is_file() or 'target' in path.parts or '__pycache__' in path.parts or path.suffix in ('.lock', '.pyc'):
                continue
            found = FORBIDDEN.findall(path.read_text(errors='replace'))
            self.assertEqual(found, [], f'{path.relative_to(ROOT)} に問題に固有の語がある')


if __name__ == '__main__':
    unittest.main()
