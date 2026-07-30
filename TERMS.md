# 追加の利用条件

本ソフトウェアの**コード**は [Apache License 2.0](LICENSE) で提供しますが、
次の条件を追加します。これ以外の使い方は Apache-2.0 の範囲で自由です。

**なぜ条件があるか**: 本ツールは数秒の音声から**実在の人物の声を再現**できます。
さらに、その声を**壊れていく音**にできます。どちらも、使い方次第で人を傷つけます。

---

## 1. 参照音声について

### 1-1. 本人の許可を得ていない声で生成しないこと

参照音声としてよいのは次のいずれかです。

- **あなた自身の声**
- **本人から利用の許可を得ている声**
- **その用途で使うことが明示的に許諾されている音声**

**実在の人物の声を、本人の許可なく複製してはいけません。**

### 1-2. 同梱の音声も「実在の人物の声」であること

`refs/` の音声は**出所が 2 種類あり、条件が違います。**

| | 出所 | 条件 |
|---|---|---|
| `refs/mossan_hoshi/` | **mossan_hoshi の声** | **CC0 ではない。**下記 1-4 |
| その他の `refs/*/` | Common Voice | **CC0 1.0 ＋「話者を特定しない」** |

Common Voice 由来のものは**素材としては自由に使えます**。ただし
**CC0 が及ぶのは録音の著作権だけ**で、話者の人格権・パブリシティ権は別です。
次のことはしないでください。

- 生成した音声を、**その話者本人の発言であるかのように扱う**
- **話者の身元を特定しようと試みる**（Common Voice の利用規約でも禁じられています）

### 1-3. 未成年の声を使わないこと

参照音声に未成年者の声を使ってはいけません。

### 1-4. mossan_hoshi の声（`refs/mossan_hoshi/`）の条件

**この音声は CC0 ではありません。mossan_hoshi の声で、
すべての権利を留保します (all rights reserved)。**

**生成して遊ぶ・聴く・保存する・共有する・SNS に投稿するといった個人利用は自由です**
（2 の禁止事項に反しない範囲で。公開するときは 3 のとおり AI 生成であることを明示）。

**制限がかかるのは音声ファイルそのもの (`refs/mossan_hoshi/*.wav`) の扱いです。**
次のことは**禁止**します。

- **音声ファイルそのものの再配布**（転載・別リポジトリへの取り込み・第三者への提供）
- **AI の学習データとしての利用**（音声モデルの学習・微調整・蒸留を含む）
- **本ツール以外のサービス・ツールへの投入**（他社の音声クローンサービスへのアップロード等）

**mossan_hoshi が求めた場合は、利用と再配布を速やかに停止してください。**

> 声そのものに著作権は生じないと解されていますが、**本項は配布条件（契約）として
> 課すもの**です。あわせて人格権・パブリシティ権も留保します。

## 2. 生成物の使い方

次の目的で使ってはいけません。

- **誹謗中傷・侮辱**: 特定の個人や団体を貶める、辱める
- **なりすまし**: 実際には言っていないことを言ったように見せる、本人を装う
- **嫌がらせ・脅迫・差別**の助長、望まない相手への送りつけ
- **本人確認の突破**: 声紋認証・電話での本人確認など、**音声を本人性の根拠にしている
  仕組みを欺くこと**
- **事故・事件・緊急事態を装うこと**: 本ツールの崩壊音声は「人が苦しんでいる」
  「意識を失っていく」ように聞こえます。**偽の救助要請や、事故・事件を装った音声に
  転用しないでください。**実害が出るうえ、救助資源を奪います
- **未成年を性的に扱う表現**への利用

## 3. 公開するときは AI 生成だと明示すること

生成した音声を第三者に見せる・配布する・投稿する場合は、
**AI で生成した合成音声であることを分かる形で示してください。**

これは礼儀の問題ではありません。EU AI Act 第 50 条をはじめ、
合成音声の開示を義務づける法規制が各国で整備されつつあります。
**適用される法令を守る責任は利用者にあります。**

## 4. 免責

本ソフトウェアは**現状有姿**で提供されます。
生成された音声、およびその利用によって生じたいかなる損害・紛争・法的責任についても、
**作者は一切の責任を負いません。利用者ご自身の責任でお使いください。**

（Apache-2.0 第 7 条・第 8 条と同趣旨ですが、**生成物にも及ぶこと**を明示します。）

## 5. 同梱アセットはライセンスの対象外

**Apache-2.0 が及ぶのはコードだけ**です。次のものは**含みません**。

| | |
|---|---|
| `assets/chibi*/` | キャラクターイラスト（作者に権利があります） |
| `assets/logo*.png` | 本アプリのロゴ |
| `assets/novtube_logo.webp` | 「のべつべ！」のロゴ |
| `samples/*.mp3` | README の表に出しているデモ音声（本ツールの生成物） |

これらを**転用・改変・再配布しないでください。**
フォークして自分のツールにする場合は、これらを差し替えてください。

**`samples/*.mp3` は実在の人物の声**（Common Voice の話者と mossan_hoshi）を
わざと壊したものです。**音声ファイル単体での再配布と、AI 学習データとしての利用を
禁止**します。聴くこと・リンクを共有することは自由です。使い方には 1-2 / 1-3 / 2 が
そのまま当てはまります（**その話者本人の発言であるかのように扱わないこと**）。

`refs/` の音声のうち **Common Voice 由来のもの**は上記の対象外です。ただし
**まったくの無条件ではありません** — **CC0 1.0 ＋「話者の特定を試みない」**
（Common Voice 利用規約）が条件です。

**`refs/mossan_hoshi/`（mossan_hoshi の声）は本項の対象**で、**転用・再配布・商用利用・
学習データ利用を禁止**します（1-4）。詳しくは [CREDITS.md](CREDITS.md) を参照してください。

---

## Additional Terms (English summary)

The **code** is licensed under the [Apache License 2.0](LICENSE), with these
additional conditions. This tool can reproduce a real person's voice from a few
seconds of audio, and can make that voice sound like it is breaking down.

**1. Reference audio**
- Only use your own voice, a voice you have permission to use, or audio
  explicitly licensed for this purpose.
- Most bundled `refs/` audio is CC0 (Common Voice), but **CC0 covers the
  recording's copyright, not the speaker's personality/publicity rights.** Do not
  present output as statements by those speakers, and do not attempt to identify them.
- **`refs/mossan_hoshi/` is NOT CC0.** It is mossan_hoshi's own voice, **all
  rights reserved**. Personal use of generated output (generate, listen, save,
  share, post to SNS) is fine, subject to section 2 below and the AI-disclosure
  rule in section 3. What's restricted is **the raw audio file itself**:
  **no redistribution of the file, no use as AI training data, no uploading it
  to other services.** Stop using and redistributing it if mossan_hoshi asks you to.
- Do not use minors' voices.

**2. Do not use generated audio to**
- defame, insult, impersonate, harass, threaten, or discriminate;
- defeat voice-based authentication or identity verification;
- **fake an accident, emergency, or call for help** — the collapse output sounds
  like a person in distress;
- sexualize minors.

**3. Disclose AI generation.** When publishing or distributing generated audio,
clearly indicate that it is AI-generated synthetic speech. Regulations such as
**EU AI Act Article 50** require this. Compliance with applicable law is your
responsibility.

**4. No liability.** Provided AS IS. The author accepts no liability for any
damage, dispute, or legal consequence arising from generated audio or its use.

**5. Bundled assets are not covered.** Character illustrations
(`assets/chibi*/`), logos (`assets/logo*.png`, `assets/novtube_logo.webp`) and
the demo audio shown in the README table (`samples/*.mp3`) are **not** licensed
under Apache-2.0. Replace them if you fork this project. The demo audio is
**real people's voices** (Common Voice speakers and mossan_hoshi), deliberately
degraded: listening and sharing links is fine, but **do not redistribute the
files as standalone audio and do not use them as AI training data** — and
sections 1 and 2 apply to them as written.
The Common Voice part of `refs/` is exempt from this, but is **not**
unconditional: it is **CC0 1.0 AND subject to Common Voice's "do not attempt to
identify speakers" rule** (see 1-2 above). **`refs/mossan_hoshi/` is covered by
this section** — all rights reserved; the audio file itself must not be
redistributed, reused, or used as training data (see 1-4). Generated output is
fine for personal use.

Because of these use-based conditions, this software is **not** "Open Source"
as defined by the Open Source Initiative, and should not be described as plain
"Apache-2.0".
