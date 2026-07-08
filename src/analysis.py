"""
馬の詳細分析・診断文生成モジュール
"""
import re
import textwrap
import numpy as np
import pandas as pd

# ---- 種牡馬特性データベース ----
# (得意距離帯, 詳細説明, 洋芝適性コメント)
SIRE_NOTES = {
    # === ディープインパクト系 ===
    'ディープインパクト': (
        '芝中距離(1600-2400m)',
        '日本競馬史上最高の種牡馬。産駒はスピードと切れ味が抜群で、高速馬場での瞬発力勝負に強い。'
        '東京・阪神の大箱で特に輝くが、洋芝でも一定の実績がある。',
        '洋芝でも好走例はあるが、高速馬場のほうが本来の能力が出やすい。',
    ),
    'キズナ': (
        '芝中距離(1800-2400m)',
        'ディープインパクトの最高傑作。産駒はスピードと持久力を兼備し、芝2000m前後で安定した成績を残す。'
        '高速馬場でも時計のかかる馬場でも対応できる汎用性が高い。古馬になって本格化する晩成型も多い。',
        '洋芝でも十分対応できる。スタミナの豊富な産駒は北海道でも好走例が多い。',
    ),
    'ワグネリアン': (
        '芝中距離(1800-2400m)',
        'ディープインパクト産駒のダービー馬。産駒は芝の中距離で安定した成績を残す。'
        '高速馬場での瞬発力に加えスタミナも兼備した万能型。',
        '洋芝でも対応できる。',
    ),
    'コントレイル': (
        '芝中距離(2000-2400m)',
        '無敗の三冠馬。初年度産駒がデビューした新種牡馬。父ディープインパクト同様、'
        '芝の中長距離でのスピードと切れ味を産駒に伝えると見られる。'
        '広いコース・長い直線でパフォーマンスが高い産駒が多い。',
        '洋芝適性は未知数だが、ディープ系の高い基礎能力で対応可能とみる。',
    ),
    'シャフリヤール': (
        '芝中距離(2000-2400m)',
        'ダービー馬・欧州GT制覇のディープ産駒の新種牡馬。産駒にスピードと持久力を伝えると期待される。'
        '高速馬場での瞬発力と重馬場での粘りを兼備した万能型血統。',
        '洋芝適性は未知だが、欧州での実績からタフな馬場にも対応できると見る。',
    ),
    'ダノンキングリー': (
        '芝マイル〜中距離(1600-2000m)',
        'ディープインパクト産駒。安田記念制覇の実力馬の後継。'
        '産駒はスピードと切れ味を兼備し、高速馬場での瞬発力勝負に強い傾向がある。',
        '洋芝での特別な強みはないが、基礎能力で対応可能。',
    ),
    'フィエールマン': (
        '芝長距離(2400-3600m)',
        'ディープインパクト産駒の天皇賞春2連覇馬。産駒は長距離・スタミナ勝負に強い。'
        '長い距離での粘り強さが最大の武器。2400m以上で特に高いパフォーマンスを示す。',
        '洋芝でも対応できる。ディープ系の柔軟性とスタミナが洋芝でも生きる。',
    ),
    # === ハーツクライ系 ===
    'ハーツクライ': (
        '芝中長距離(2000-2500m)',
        '晩成傾向で古馬になって本格化するタイプが多い。産駒は斤量を背負っても崩れない底力があり、'
        '長い直線よりも小回りコースで堅実に走る傾向がある。',
        '洋芝でも十分対応できる。タフな馬場ほど持ち味のスタミナが生きる。',
    ),
    'サリオス': (
        '芝マイル〜中距離(1600-2000m)',
        'ハーツクライ産駒。朝日杯FS圧勝の実力馬の後継。産駒はスピードとスタミナのバランスに優れ、'
        'マイル〜中距離で安定した成績を残す。晩成傾向のハーツ系を受け継ぐ可能性も。',
        '洋芝でも対応できる。ハーツクライ系のスタミナは洋芝でも生きる。',
    ),
    # === ロードカナロア系 ===
    'ロードカナロア': (
        '芝・ダート短中距離(1200-1600m)',
        'スプリント〜マイルに圧倒的な実績。産駒はスピードに優れ、芝・ダートを問わず活躍。'
        '2000m以上は距離的に苦しい傾向がある。',
        '洋芝対応は可能だが、距離が伸びるほど不安が増す。',
    ),
    'サートゥルナーリア': (
        '芝中距離(1600-2400m)',
        'ロードカナロア産駒。産駒はスピードと持久力を兼備し、幅広い条件に対応できる。'
        '高速馬場での瞬発力と持続力を産駒に伝えると期待される。',
        '洋芝でも対応できる。',
    ),
    # === キングカメハメハ系 ===
    'キングカメハメハ': (
        '芝・ダート中距離(1600-2200m)',
        'オールラウンダーで芝・ダート両方に強い万能型種牡馬。中距離を中心に幅広い条件で活躍。'
        '産駒は安定感があり崩れにくい。洋芝適性も普通以上。',
        '洋芝コースでも安定した成績を残せる。',
    ),
    'ドゥラメンテ': (
        '芝中距離(1800-2200m)',
        '高速馬場での瞬発力勝負が産駒の最大の武器。東京・阪神の直線が長いコースで特に輝く。'
        '洋芝でのスピード勝負になると、高速馬場ほどの切れ味が活かしにくい可能性がある。',
        '洋芝は苦手なわけではないが、高速馬場での切れ味という強みが活かしにくい。',
    ),
    'ルーラーシップ': (
        '芝中距離(1800-2400m)',
        'キングカメハメハ産駒。産駒はパワーと持久力を武器に中距離で安定した成績を残す。'
        '欧州血統との配合も豊富で、力のいる馬場や大箱コースに適性がある産駒が多い。',
        '洋芝でも十分対応できる。欧州血統が入った産駒は特に洋芝に強い。',
    ),
    'レイデオロ': (
        '芝中長距離(2000-2600m)',
        'キングカメハメハ産駒のダービー馬。産駒はスタミナとパワーを武器に中長距離で活躍。'
        '広いコース・長い直線でのパフォーマンスが高い。斤量を背負っても崩れにくい底力が産駒の強み。',
        '洋芝でも問題なく対応できる。',
    ),
    # === エピファネイア系 ===
    'エピファネイア': (
        '芝中長距離(2000-2600m)',
        '産駒はパワー型でスタミナが豊富。道悪・重馬場でも崩れない持続力がある。'
        '洋芝コース（北海道）との相性も良く、函館・札幌で好成績を残す産駒が多い。',
        '洋芝で十分な実績がある。パワー型の血統は洋芝の消耗戦で強さを発揮。',
    ),
    'エフフォーリア': (
        '芝中距離(1800-2200m)',
        'エピファネイア産駒の2021年最優秀3歳牡馬・天皇賞秋制覇馬の後継。'
        'パワーと持続力を武器に中距離での安定感が高い産駒が多いと予測される。'
        'エピファネイア系特有の洋芝・重馬場への適性を引き継ぐ可能性が高い。',
        '洋芝・重馬場での好走が期待できる血統。父エピファネイアの特性を継承。',
    ),
    # === キタサンブラック系 ===
    'キタサンブラック': (
        '芝中長距離(2000-3200m)',
        '産駒はスタミナと底力が豊富。洋芝コース（北海道）との相性が抜群で、'
        'タフな馬場・長い距離でも崩れない粘り強さが最大の武器。2000m以上の重賞で特に威力を発揮。',
        '★ 洋芝との相性は種牡馬の中でもトップクラス。今回の函館・洋芝は「ど真ん中」の条件。',
    ),
    # === モーリス系 ===
    'モーリス': (
        '芝マイル〜中距離(1600-2000m)',
        'マイルから2000mが産駒のベスト距離。鋭い末脚と安定感を兼備。'
        '高速馬場でも時計のかかる馬場でも対応できる適応力の高さが特徴。',
        '洋芝でも普通に対応できる。',
    ),
    # === オルフェーヴル・ゴールドシップ系（ステイゴールド系） ===
    'ステイゴールド': (
        '芝中長距離(2000-3600m)',
        'ゴールドシップ・オルフェーヴルの父。産駒はタフで消耗戦に強い。'
        '長距離・道悪・洋芝という厳しい条件ほど本領を発揮するタイプが多い。'
        '気性が激しい産駒も多いが、本番に強い精神力を受け継ぐ。',
        '★ 洋芝との相性は優秀。消耗戦・道悪でより一層の強さを発揮する産駒が多い。',
    ),
    'オルフェーヴル': (
        '芝中長距離(2000-3600m)',
        'タフな条件を最も得意とする種牡馬。洋芝・重馬場でも好走し、長距離での底力は産駒最大の武器。'
        '小回りの函館コースでも十分対応できるスタミナ型が多い。',
        '★ 洋芝・タフな条件で特に輝く。函館・洋芝はまさに「得意条件」。',
    ),
    'ゴールドシップ': (
        '芝中長距離(2200-3600m)',
        '洋芝・タフな条件で特に威力を発揮する種牡馬。産駒はスタミナ豊富で、'
        '洋芝の函館・札幌での高成績が目立つ。',
        '★ 洋芝との相性は産駒の中でも特に優れる。今回は血統的に大きなアドバンテージ。',
    ),
    'インディチャンプ': (
        '芝マイル(1400-1800m)',
        'ステイゴールド産駒のマイル王。産駒はスピードと底力を兼備。'
        'マイル〜短距離を中心にパンチのある末脚で勝負するタイプが多い。',
        '洋芝でも対応できる。ステイゴールド系の耐久力は洋芝でも生きる。',
    ),
    # === 欧州系輸入種牡馬 ===
    'ハービンジャー': (
        '芝中長距離(2000-3200m)',
        '英国のダンシリ系種牡馬。産駒は洋芝・時計のかかる馬場で圧倒的な実績を誇る。'
        '北海道（函館・札幌）での成績が特に高く、洋芝では日本全体でもトップクラスの血統。'
        'スタミナ豊富でペースが流れる消耗戦に強い。重馬場もこなす。',
        '★★ 洋芝との相性は日本の種牡馬中でも最高クラス。洋芝では血統的に大きなアドバンテージ。',
    ),
    'フランケル': (
        '芝マイル〜中距離(1600-2200m)',
        '英国の歴史的名馬。欧州型の高いスピードと持続力を産駒に伝える。'
        '日本でも芝の中距離で高い実績を誇り、高速馬場でも時計のかかる馬場でも対応できる万能性がある。'
        '差し脚の鋭い産駒が多い。',
        '洋芝との相性も良好。欧州型血統の産駒は時計のかかる馬場で真価を発揮。',
    ),
    'Frankel': (
        '芝マイル〜中距離(1600-2200m)',
        '英国の歴史的名馬フランケル産駒。欧州型の高いスピードと持続力を産駒に伝える。'
        '日本でも芝の中距離で高い実績を誇り、万能性がある。',
        '洋芝との相性も良好。欧州型血統の産駒は時計のかかる馬場で真価を発揮。',
    ),
    'グレナディアガーズ': (
        '芝マイル(1400-1800m)',
        'フランケル産駒。NHKマイルカップ制覇の新種牡馬。'
        '産駒はスピードと切れ味に優れ、芝の短中距離で高いパフォーマンスを見せる。'
        'マイルを中心に1400-1800mの距離をコンパクトに走る産駒が多い。',
        '洋芝でも対応できる。フランケル系の特性を受け継ぎ、重い馬場にも対応力あり。',
    ),
    # === ダイワメジャー系 ===
    'ダイワメジャー': (
        '芝マイル(1400-1800m)',
        'サンデーサイレンス系のマイル専門種牡馬。産駒はタフで気性が強く、'
        '激しい流れの中でも崩れない安定感が武器。芝1400-1800mに圧倒的な実績がある。'
        '体力があり斤量を背負っても崩れにくい安定感が強み。',
        '洋芝での特別な強みはないが、タフな産駒は標準的に対応できる。',
    ),
    # === その他芝系 ===
    'リアルスティール': (
        '芝中距離(1800-2200m)',
        '産駒は安定した成績を残す堅実派。芝中距離を中心に活躍し、2000m前後がベスト。'
        '爆発的な切れ味はないが、崩れにくく連対率が高い傾向がある。',
        '洋芝での特別な強さはないが、標準的な対応力はある。',
    ),
    'モズアスコット': (
        '芝短距離〜マイル(1200-1600m)',
        'スピード特化型の種牡馬。産駒は1200-1600mの速い流れを好む傾向がある。'
        '2000mは距離的にやや長く、今回の条件は若干の不安材料となる。',
        '洋芝でも走れるが、距離延長がカギ。',
    ),
    'ジャスタウェイ': (
        '芝マイル〜中距離(1600-2000m)',
        '産駒は切れ味とスタミナのバランスが良く、2000m前後で特に安定した成績を残す。'
        '斤量が重くなっても崩れにくい持続力が特徴。',
        '洋芝でも問題なく対応できる。',
    ),
    'スワーヴリチャード': (
        '芝中距離(1800-2400m)',
        '産駒はパワーと末脚を兼備。2000m前後の芝中距離を安定して走るタイプが多い。'
        '道悪や力のいる馬場にも強い。',
        '洋芝での適性も十分ある。',
    ),
    'シルバーステート': (
        '芝マイル〜中距離(1600-2000m)',
        '産駒は成長力があり、古馬になって真価を発揮するタイプが多い。'
        '芝の中距離で高成績を残しており、2000m前後がベスト。',
        '洋芝でも問題なく対応できる。',
    ),
    'サトノダイヤモンド': (
        '芝中長距離(2000-3000m)',
        '産駒はスタミナを武器に長い距離をこなす。洋芝との相性も良く、'
        '函館・札幌でも好走例が多い。',
        '洋芝との相性は良好。',
    ),
    'イクイノックス': (
        '芝中距離(2000-2400m)',
        '史上最強馬の産駒は将来性豊か。父の特性を受け継ぎ、芝の中長距離で高いパフォーマンスが期待できる。',
        '洋芝適性は未知数だが、高い基礎能力で対応可能と見る。',
    ),
    'ブリックスアンドモルタル': (
        '芝中距離(1800-2200m)',
        '産駒は切れ味と持続力を兼備。2000m前後の芝中距離がベスト。'
        '洋芝でも安定した成績を残せるタイプ。',
        '洋芝での適性は問題ない。',
    ),
    'スクリーンヒーロー': (
        '芝中長距離(1800-2600m)',
        'ジェンティルドンナの父。産駒はタフで距離適性が広く、芝の中長距離を中心に活躍。'
        '古馬になって本格化するタイプが多く、G1での活躍馬も多数輩出。'
        '道悪・重馬場でも崩れにくいパワー型。',
        '洋芝での適性も十分。スタミナ型血統は消耗戦で強さを発揮。',
    ),
    'アドマイヤムーン': (
        '芝中距離(1800-2200m)',
        '産駒は2000m前後の芝中距離を安定して走るタイプが多い。'
        'スピードと持続力のバランスが良く、コーナーでの手応えが良い産駒が多い。'
        '洋芝・野芝問わず堅実な成績を残せる万能型。',
        '洋芝でも問題なく対応できる。',
    ),
    'マンハッタンカフェ': (
        '芝長距離(2200-3600m)',
        'スタミナ特化型の種牡馬。産駒は長距離・菊花賞路線での活躍が顕著。'
        'オジュウチョウサン（障害G1 7連覇）の父でもある。'
        '2400m以上の長距離で突出した実績を持つ血統。',
        '洋芝でも十分に対応できる。スタミナ型は洋芝のタフな条件で強さを発揮。',
    ),
    'サトノクラウン': (
        '芝中長距離(2000-2600m)',
        '香港ヴァーズ制覇の欧州血統系種牡馬。産駒はパワーとスタミナを武器に'
        '重馬場・道悪でも崩れにくい耐久性が特徴。'
        '2000m以上の中長距離をコースを問わず安定して走る。',
        '洋芝での実績が高い。重い馬場・消耗戦での粘りは血統的な強み。',
    ),
    'ヴィクトワールピサ': (
        '芝中距離(1800-2400m)',
        'UAE ドバイワールドカップも制した実力馬の後継。産駒は芝中距離で安定した成績を残す。'
        'パワー型の血統で重い馬場・洋芝でも実績がある。',
        '洋芝でも対応できる。',
    ),
    # === ダート系 ===
    'ヘニーヒューズ': (
        'ダート短中距離(1200-1800m)',
        'ダート専門の最強系種牡馬。産駒はダートの短中距離で圧倒的な実績を誇る。'
        '芝の重賞では通常苦しい。今回の芝2000mは苦しい条件。',
        '洋芝への適性は非常に低い（ダート専門血統）。',
    ),
    'ホッコータルマエ': (
        'ダート中距離(1600-2100m)',
        'ダートの中距離が得意。産駒はダートのパワー勝負に強い。'
        '芝の重賞では通常苦しい条件となる。',
        '洋芝への適性は低い（ダート専門血統）。',
    ),
    'カフェファラオ': (
        'ダート中距離(1400-1800m)',
        'American Pharoah産駒のダート中距離王。フェブラリーS2連覇。'
        '産駒はダートのスピード勝負に強い。芝レースでの実績はほとんどない。',
        '洋芝への適性は低い（ダート専門血統）。',
    ),
    'ドレフォン': (
        'ダートスプリント(1200-1600m)',
        '米国産のダートスプリント特化種牡馬。産駒は短距離ダートで爆発的なスピードを持つ。'
        '芝レースには向かない純粋なダート血統。',
        '洋芝への適性は非常に低い（ダートスプリント血統）。',
    ),
    'アメリカンファラオ': (
        'ダート中距離(1600-2000m)',
        '米国三冠馬。産駒はダートの中距離で安定した実績を持つ。'
        '芝でも走れる産駒がいるが、本質はダート中距離。',
        '洋芝への適性は低い（ダート系統）。ただし芝適性がある産駒は例外。',
    ),
    'ニューイヤーズデイ': (
        'ダート短距離(1200-1600m)',
        '米国産ダート短距離種牡馬。産駒は短距離ダートで抜群のスピードを見せる。'
        '芝レースへの適性は低く、ダートの短中距離が主戦場。',
        '洋芝への適性は非常に低い（ダート専門血統）。',
    ),
    'サウスヴィグラス': (
        'ダート短距離(1000-1400m)',
        '地方・JRAダート短距離の最強種牡馬の一頭。産駒はダートのスプリント戦に特化した'
        '爆発的なスピードを持ち、1000-1400mで特に高い勝率を誇る。'
        '芝レースでの活躍はまれ。',
        '洋芝への適性は非常に低い（ダートスプリント専門）。',
    ),
    # === 万能系 ===
    'クロフネ': (
        '芝・ダート万能(1400-2000m)',
        '芝・ダート双方で活躍する万能血統。特にダートマイルでの実績が光るが、'
        '芝でも好走できる柔軟性がある。',
        '洋芝でも対応できる。',
    ),
}

# 洋芝（北海道）で特に強い父
RYOYO_SIRES = {
    'キタサンブラック', 'オルフェーヴル', 'ゴールドシップ', 'エピファネイア',
    'ハーツクライ', 'ディープインパクト', 'キングカメハメハ', 'サトノダイヤモンド',
    'モーリス', 'ジャスタウェイ', 'スワーヴリチャード', 'シルバーステート',
    # 追加: 洋芝実績の高い種牡馬
    'ハービンジャー', 'ステイゴールド', 'キズナ', 'ルーラーシップ',
    'フランケル', 'Frankel', 'サトノクラウン', 'スクリーンヒーロー',
    'エフフォーリア', 'フィエールマン', 'マンハッタンカフェ',
}

# 洋芝コード（函館・札幌）
RYOYO_VENUES = {'01', '02'}  # 01=札幌, 02=函館

VENUE_NAME = {
    '01': '札幌', '02': '函館', '03': '福島', '04': '新潟',
    '05': '東京', '06': '中山', '07': '中京',
    '08': '京都', '09': '阪神', '10': '小倉',
}

# 距離が長い → 短いに強い父かどうか
STAY_SIRES  = {
    'キタサンブラック', 'ハーツクライ', 'オルフェーヴル', 'ゴールドシップ',
    'エピファネイア', 'サトノダイヤモンド',
    'ステイゴールド', 'マンハッタンカフェ', 'フィエールマン', 'サトノクラウン',
}
SPEED_SIRES = {
    'ロードカナロア', 'モズアスコット', 'ヘニーヒューズ', 'サウスヴィグラス',
    'ダイワメジャー', 'ドレフォン', 'カフェファラオ', 'ニューイヤーズデイ', 'グレナディアガーズ',
}


def parse_grade(race_name):
    race_name = str(race_name)
    if not race_name or race_name in ('nan', 'None'):
        return ''
    if re.search(r'G1|GI(?!I)|グランプリ', race_name):
        return 'G1'
    if re.search(r'G2|GII(?!I)', race_name):
        return 'G2'
    if re.search(r'G3|GIII', race_name):
        return 'G3'
    if re.search(r'\(L\)|リステッド', race_name):
        return 'L'
    if re.search(r'オープン|OP|特別', race_name):
        return 'OP'
    return '条件'


def dist_band_label(d):
    try:
        d = int(d)
        if d <= 1400:   return '短距離(〜1400m)'
        if d <= 1800:   return 'マイル(1401-1800m)'
        if d <= 2200:   return '中距離(1801-2200m)'
        return '長距離(2200m〜)'
    except Exception:
        return '不明'


def _parse_race_time(s):
    """タイム文字列を秒に変換: '1:35.2' → 95.2  '35.2' → 35.2"""
    s = str(s).strip()
    if not s or s in ('nan', 'None', '---', '--', ''):
        return np.nan
    m = re.match(r'(\d+):(\d+\.\d+)', s)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.match(r'(\d+\.\d+)', s)
    if m:
        return float(m.group(1))
    return np.nan


def _build_par_times(races_df):
    """surface×distance ごとの勝ち馬タイム中央値（パータイム）を返す"""
    if races_df.empty or 'time' not in races_df.columns:
        return {}
    r = races_df.copy()
    r['_pos'] = pd.to_numeric(r['finishing_pos'], errors='coerce')
    r['_t']   = r['time'].apply(_parse_race_time)
    r['_d']   = pd.to_numeric(r['distance'], errors='coerce')
    winners   = r[(r['_pos'] == 1) & r['_t'].notna() & r['_d'].notna()]
    par = winners.groupby(['surface', '_d'])['_t'].median()
    return {f"{surf}_{int(d)}": t for (surf, d), t in par.items()}


def build_history_df(horse_name, races_df):
    """races.csvから特定の馬のJRAレース履歴のみを取得"""
    if races_df.empty:
        return pd.DataFrame()
    h = races_df[races_df['horse_name'] == horse_name].copy()
    if h.empty:
        return pd.DataFrame()

    # JRAレースのみに絞る（venue code 01-10）
    h['_vc'] = h['race_id'].astype(str).str[4:6]
    h = h[h['_vc'].isin({'01','02','03','04','05','06','07','08','09','10'})].copy()

    # race_dateの扱い:
    # CSVのrace_dateはスクレイプ前はrace_id[:8](=YYYY+会場+開催回)が入っており日付ではない
    # scraper修正後・repair後はYYYYMMDD形式の実際の日付が入る
    # race_date == race_id[:8] の場合は未確定（race_idから仮設定）
    h['race_id_s'] = h['race_id'].astype(str)
    h['race_date_s'] = h['race_date'].astype(str).str.strip().str[:8]
    h['has_real_date'] = h['race_date_s'] != h['race_id_s'].str[:8]

    # 年は race_id[:4] が確実なので、ソートキーとして race_id を使う
    h = h.sort_values('race_id_s', ascending=False).reset_index(drop=True)

    h['pos']    = pd.to_numeric(h['finishing_pos'], errors='coerce')
    h['dist']   = pd.to_numeric(h['distance'], errors='coerce')
    h['pop']    = pd.to_numeric(h['popularity'], errors='coerce')
    h['odds_f'] = pd.to_numeric(h['odds'], errors='coerce')
    h = h.dropna(subset=['pos'])
    h['pos'] = h['pos'].astype(int)

    # 同一race_idの重複を除去
    h = h.drop_duplicates(subset=['race_id', 'horse_name'])
    h = h.sort_values('race_id_s', ascending=False).reset_index(drop=True)

    # タイム指数: 勝ち馬中央値との比較で補正速度指数を計算
    par_times = _build_par_times(races_df)
    if par_times:
        h['_t_s']    = h['time'].apply(_parse_race_time)
        h['_dist_n'] = pd.to_numeric(h['distance'], errors='coerce')
        def _sidx(row):
            key = f"{row.get('surface', '')}_{int(row['_dist_n']) if pd.notna(row.get('_dist_n')) else 0}"
            par = par_times.get(key)
            t   = row.get('_t_s')
            if par and pd.notna(t) and par > 0:
                return round((par - t) / par * 100 + 80, 1)
            return np.nan
        h['speed_idx'] = h.apply(_sidx, axis=1)
        h = h.drop(columns=['_t_s', '_dist_n'])
    else:
        h['speed_idx'] = np.nan

    return h


def _result_str(sub):
    """小DFから n戦w勝（3着内率XX%）を返す"""
    n = len(sub)
    if n == 0:
        return None
    w = int((sub['pos'] == 1).sum())
    t = int((sub['pos'] <= 3).sum())
    rate = t / n * 100
    return f'{n}戦{w}勝（3着内率{rate:.0f}%）'


def _pos_icon(pos):
    return {1: '1着◎', 2: '2着○', 3: '3着▲'}.get(pos, f'{pos}着')


def _form_string(history_df, n=10):
    """近n走の着順を実数字で表示: 1→2→3→...（直近→古い）"""
    result = []
    for pos in history_df['pos'].head(n):
        result.append(str(int(pos)) + '着')
    return '→'.join(result) + '（直近→古い）'


def generate_analysis(horse_name, row, pedigree, history_df, race_surface, race_distance, race_venue_code):
    """詳細な診断文を生成してリストで返す"""
    lines = []
    sire     = pedigree.get('sire', '') if pedigree else ''
    dam_sire = pedigree.get('dam_sire', '') if pedigree else ''
    is_ryoyo = race_venue_code in RYOYO_VENUES
    venue_lbl = '洋芝(函館・札幌)' if is_ryoyo else '野芝'

    try:
        rd = int(race_distance)
    except Exception:
        rd = 0

    # ================================================================
    # 【血統診断】
    # ================================================================
    lines.append('【血統診断】')
    if sire:
        lines.append(f'  父: {sire}  /  母父: {dam_sire or "不明"}')
        lines.append('')

        sire_info = SIRE_NOTES.get(sire)
        if sire_info:
            best_dist, sire_desc, ryoyo_note = sire_info
            # ハイフン付き数字を壊さないよう空白位置で折り返す
            for l in textwrap.wrap(sire_desc, 60, break_on_hyphens=False):
                lines.append(f'  {l}')
            lines.append('')
            # 今回条件との適合評価
            if is_ryoyo:
                lines.append(f'  {ryoyo_note}')
            # 距離適性コメント
            if rd > 0:
                if sire in STAY_SIRES and rd >= 1800:
                    lines.append(f'  → 距離{race_distance}mは血統的に◎（スタミナ型）')
                elif sire in SPEED_SIRES and rd >= 2000:
                    lines.append(f'  → 距離{race_distance}mは血統的に△（スピード型産駒のため）')
                elif rd >= 2000:
                    pass  # 中距離以上は距離適性コメントなし（一般的に対応可能）
        else:
            lines.append(f'  {sire}産駒（詳細データ未登録）')

        if dam_sire and dam_sire in SIRE_NOTES:
            d_best, d_desc, _ = SIRE_NOTES[dam_sire]
            lines.append('')
            lines.append(f'  母父 {dam_sire}: {d_best}に実績。')
            first = d_desc.split('。')[0] + '。'
            lines.append(f'  {first[:60]}')
    else:
        lines.append('  血統データ取得不可（--no-pedigreeモード または スクレイプ失敗）')
    lines.append('')

    # ================================================================
    # 【成績・適性】
    # ================================================================
    has_surf_data = False
    if not history_df.empty:
        surf_vals = history_df['dist'].dropna()
        has_surf_data = len(surf_vals) > 0

    n_hist = len(history_df)

    # データ期間ラベルを動的に生成
    if n_hist > 0:
        years = history_df['race_id_s'].str[:4].dropna().unique()
        years = sorted([y for y in years if y.isdigit()])
        if years:
            data_label = f'※{years[0]}〜{years[-1]}年データ' if years[0] != years[-1] else f'※{years[0]}年データ'
        else:
            data_label = ''
    else:
        data_label = ''
    lines.append(f'【成績・適性】{data_label}')

    if n_hist == 0:
        lines.append('  データなし（データ期間外のデビュー、またはNARのみ出走）')
    else:
        # 通算
        w   = int((history_df['pos'] == 1).sum())
        p   = int((history_df['pos'] == 2).sum())
        s   = int((history_df['pos'] == 3).sum())
        oof = n_hist - w - p - s
        win_rate  = w / n_hist * 100
        t3_rate   = (w + p + s) / n_hist * 100
        lines.append(f'  通算: {n_hist}戦{w}勝  [{w}-{p}-{s}-{oof}]  '
                     f'（勝率{win_rate:.0f}% / 3着内率{t3_rate:.0f}%）')

        # 洋芝コース実績（venue_codeから確実にわかる）
        if is_ryoyo:
            h_ryoyo = history_df[history_df['race_id'].str[4:6].isin(RYOYO_VENUES)]
            if not h_ryoyo.empty:
                rr = _result_str(h_ryoyo)
                t3r = (h_ryoyo['pos'] <= 3).sum() / len(h_ryoyo) * 100
                mark = '◎' if t3r >= 50 else ('○' if t3r >= 33 else '△')
                lines.append(f'  洋芝(函館・札幌): {rr} {mark}')
            else:
                lines.append('  洋芝(函館・札幌): 出走実績なし（今回が初の洋芝コース）')

        # 馬場別・距離帯別（repair後に有効になる）
        if has_surf_data:
            turf = history_df[history_df['surface'] == '芝']
            dirt = history_df[history_df['surface'] == 'ダ']
            if len(turf) > 0:
                mark_t = ' ←今回' if race_surface == '芝' else ''
                lines.append(f'  芝: {_result_str(turf)}{mark_t}')
            if len(dirt) > 0:
                mark_d = ' ←今回' if race_surface == 'ダ' else ''
                lines.append(f'  ダート: {_result_str(dirt)}{mark_d}')

            if rd > 0:
                # dist列はfloat型・NaN混在のため notna()フィルタを必ず通す
                dist_known = history_df[history_df['dist'].notna()].copy()
                if rd <= 1400:   d_mask = dist_known['dist'] <= 1400
                elif rd <= 1800: d_mask = (dist_known['dist'] > 1400) & (dist_known['dist'] <= 1800)
                elif rd <= 2200: d_mask = (dist_known['dist'] > 1800) & (dist_known['dist'] <= 2200)
                else:            d_mask = dist_known['dist'] > 2200
                sd = dist_known[d_mask]
                band_lbl = dist_band_label(rd)
                if not sd.empty:
                    t3r = (sd['pos'] <= 3).sum() / len(sd) * 100
                    mark = '◎' if t3r >= 50 else ('○' if t3r >= 33 else '△')
                    lines.append(f'  {band_lbl}: {_result_str(sd)} {mark} ← 今回距離帯')
                elif len(dist_known) > 0:
                    lines.append(f'  {band_lbl}: 出走実績なし（今回が初の距離帯）')
        else:
            lines.append('  ※芝/ダート・距離帯の詳細は「8_データ補修」実行後に表示されます')

        # フォーム文字列（近走を一目で）
        if n_hist > 1:
            lines.append(f'  近走フォーム: {_form_string(history_df)}')

    lines.append('')

    # ================================================================
    # 【近走詳細】直近5走
    # ================================================================
    if n_hist > 0:
        lines.append('【近走詳細】')
        for _, r_row in history_df.head(5).iterrows():
            pos      = r_row['pos']
            rid      = str(r_row.get('race_id', ''))
            venue_c  = rid[4:6] if len(rid) >= 6 else ''
            venue    = VENUE_NAME.get(venue_c, f'会場{venue_c}' if venue_c else '不明')
            rname_raw = str(r_row.get('race_name', ''))
            rname    = '' if rname_raw in ('nan', 'None', '') else rname_raw[:16]
            surf     = str(r_row.get('surface', ''))
            surf     = '' if surf in ('nan', 'None') else surf
            dist_raw = r_row.get('dist')
            dist_v   = str(int(dist_raw)) if pd.notna(dist_raw) else ''
            rid      = str(r_row.get('race_id', ''))
            has_real = r_row.get('has_real_date', False)
            if has_real:
                date_v  = str(r_row.get('race_date_s', ''))[:8]
                date_fmt = f"{date_v[:4]}/{date_v[4:6]}" if len(date_v) >= 6 else date_v[:4]
            else:
                # race_dateが未確定（会場コードが混入）→ 年のみ表示
                date_fmt = rid[:4]
            pop_v    = r_row.get('pop')
            jock     = str(r_row.get('jockey', ''))
            jock     = '' if jock == 'nan' else jock
            grade    = parse_grade(rname) if rname else ''

            pop_str   = f'{int(pop_v)}人気' if pd.notna(pop_v) else ''
            grade_str = f'[{grade}]' if grade and grade not in ('条件',) else ''

            # コース表示：surface+distance or race_name or 会場のみ
            if surf and dist_v:
                course = f'{surf}{dist_v}m'
            elif rname:
                course = rname
            else:
                # 会場だけ（補修前の状態）
                is_ryoyo_race = venue_c in RYOYO_VENUES
                course = f'{"洋芝" if is_ryoyo_race else "野芝"}{"(" + venue + ")" if venue else ""}'

            pos_str  = _pos_icon(pos)
            jock_str = f'  [{jock}]' if jock else ''

            # タイム指数
            si_val = r_row.get('speed_idx')
            if pd.notna(si_val):
                si_mark = ('◎' if si_val >= 82 else ('○' if si_val >= 79 else '△'))
                si_str  = f'  指数{si_val:.1f}{si_mark}'
            else:
                si_str = ''

            lines.append(
                f'  {date_fmt} {venue} {course}{grade_str}  {pos_str}  {pop_str}{jock_str}{si_str}'
            )

            # 洋芝でのコメント
            if venue_c in RYOYO_VENUES:
                if pos <= 3:
                    lines.append(f'    Good 洋芝({venue})で好走 -> 今回条件と類似')
                else:
                    lines.append(f'    Poor 洋芝({venue})で凡走 -> 洋芝適性に疑問')

        lines.append('')

    # ================================================================
    # 【今回条件との適合まとめ】
    # ================================================================
    lines.append('【今回条件との適合まとめ】')
    evals = []

    # 血統評価
    if sire:
        if is_ryoyo and sire in RYOYO_SIRES:
            evals.append(f'◎ 血統({sire}): 洋芝との相性◎、今回の最大の強み')
        elif is_ryoyo:
            sire_ryoyo_note = SIRE_NOTES.get(sire, ('', '', '洋芝適性は標準的'))[2]
            evals.append(f'○ 血統({sire}): {sire_ryoyo_note[:30]}')
        else:
            if sire in STAY_SIRES and rd >= 2000:
                evals.append(f'◎ 血統({sire}): スタミナ型、中長距離に最適')
            elif sire in SPEED_SIRES and rd >= 2000:
                evals.append(f'△ 血統({sire}): スピード型、距離{race_distance}mは長い可能性')
            else:
                evals.append(f'○ 血統({sire}): 今回距離に対応できる血統')

    # 洋芝実績
    if is_ryoyo and n_hist > 0:
        h_ryoyo = history_df[history_df['race_id'].str[4:6].isin(RYOYO_VENUES)]
        if not h_ryoyo.empty:
            t3r = (h_ryoyo['pos'] <= 3).sum() / len(h_ryoyo) * 100
            mark = '◎' if t3r >= 50 else ('○' if t3r >= 33 else '△')
            evals.append(f'{mark} 洋芝実績: {len(h_ryoyo)}戦 3着内率{t3r:.0f}%')
        else:
            evals.append('? 洋芝: 未経験（未知数）')

    # 芝/距離（補修済みデータがある場合）
    if has_surf_data:
        surf_h = history_df[history_df['surface'] == race_surface]
        if not surf_h.empty:
            sr = (surf_h['pos'] <= 3).sum() / len(surf_h) * 100
            mark = '◎' if sr >= 50 else ('○' if sr >= 33 else '△')
            evals.append(f'{mark} {race_surface}適性: {_result_str(surf_h)}')

    # 近走フォーム評価
    if n_hist > 0:
        recent3 = history_df['pos'].head(3).tolist()
        avg3 = sum(recent3) / len(recent3)
        if avg3 <= 3:
            evals.append(f'◎ 近走フォーム良好: 直近{len(recent3)}走平均{avg3:.1f}着')
        elif avg3 <= 6:
            evals.append(f'○ 近走フォーム普通: 直近{len(recent3)}走平均{avg3:.1f}着')
        else:
            evals.append(f'△ 近走フォーム不振: 直近{len(recent3)}走平均{avg3:.1f}着')

    for e in evals:
        lines.append(f'  {e}')

    if not has_surf_data:
        lines.append('  ※ 「8_データ補修.bat」を実行すると芝/距離適性も表示されます')

    return lines
