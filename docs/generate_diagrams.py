import os
import cairosvg

def create_action_flow_svg():
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="700" viewBox="0 0 1000 700">
  <defs>
    <!-- Background Gradient -->
    <linearGradient id="bg-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#14151a"/>
      <stop offset="100%" stop-color="#0a0a0d"/>
    </linearGradient>
    
    <!-- Gold Gradient -->
    <linearGradient id="gold-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#B8960C"/>
    </linearGradient>
    
    <!-- Teal Gradient -->
    <linearGradient id="teal-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2DD4BF"/>
      <stop offset="100%" stop-color="#0D9488"/>
    </linearGradient>

    <!-- Gold to Teal Gradient (Horizontal) -->
    <linearGradient id="gold-teal-grad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#2DD4BF"/>
    </linearGradient>

    <!-- Teal to Gold Gradient (Horizontal) -->
    <linearGradient id="teal-gold-grad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#2DD4BF"/>
      <stop offset="100%" stop-color="#D4A017"/>
    </linearGradient>
    
    <!-- Box Background -->
    <linearGradient id="box-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(30, 31, 38, 0.4)"/>
      <stop offset="100%" stop-color="rgba(15, 15, 20, 0.6)"/>
    </linearGradient>
    
    <!-- Shadow Filter -->
    <filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="8" stdDeviation="6" flood-color="#000000" flood-opacity="0.6"/>
    </filter>

    <!-- Arrow Markers -->
    <marker id="arrow-gold" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#D4A017"/>
    </marker>
    <marker id="arrow-teal" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#2DD4BF"/>
    </marker>
  </defs>

  <!-- Background -->
  <rect width="1000" height="700" fill="url(#bg-grad)"/>

  <!-- Title -->
  <text x="50" y="60" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="26" font-weight="bold">電脳体モデル アクション遷移図</text>
  <text x="50" y="85" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="14">あかねの物理体モードと電脳体モードの行き来と4つの基本アクション</text>

  <!-- 1. PHYSICAL MODE CONTAINER -->
  <g filter="url(#shadow)">
    <rect x="60" y="130" width="380" height="500" rx="16" fill="url(#box-bg)" stroke="url(#gold-grad)" stroke-width="2"/>
    <!-- Container Title -->
    <text x="90" y="175" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="20" font-weight="bold">物理体モード (Physical Mode)</text>
    <text x="90" y="202" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13">部屋グラフ上に存在。音声の錨となる身体</text>
    
    <!-- State Var -->
    <rect x="90" y="225" width="320" height="40" rx="6" fill="rgba(0, 0, 0, 0.3)" stroke="rgba(212, 160, 23, 0.3)" stroke-width="1"/>
    <text x="105" y="250" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold">位置:</text>
    <text x="145" y="250" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold">projected_room = null</text>
    
    <!-- Room A Node -->
    <rect x="90" y="370" width="130" height="90" rx="10" fill="rgba(20, 21, 26, 0.8)" stroke="#B8960C" stroke-width="1.5"/>
    <text x="155" y="410" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="15" font-weight="bold" text-anchor="middle">部屋 A</text>
    <text x="155" y="435" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">study (現在室)</text>
    
    <!-- Room B Node -->
    <rect x="280" y="370" width="130" height="90" rx="10" fill="rgba(20, 21, 26, 0.8)" stroke="#B8960C" stroke-width="1.5"/>
    <text x="345" y="410" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="15" font-weight="bold" text-anchor="middle">部屋 B</text>
    <text x="345" y="435" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">living_room</text>
    
    <!-- Action 1: move_to(room) -->
    <!-- Arrow Right -->
    <path d="M 220 395 Q 250 385 280 395" fill="none" stroke="url(#gold-grad)" stroke-width="2" marker-end="url(#arrow-gold)"/>
    <!-- Arrow Left -->
    <path d="M 280 415 Q 250 425 220 415" fill="none" stroke="url(#gold-grad)" stroke-width="2" marker-end="url(#arrow-gold)"/>
    
    <!-- Background for action text to prevent overlapping issues -->
    <rect x="200" y="325" width="100" height="20" rx="4" fill="rgba(10, 10, 15, 0.8)"/>
    <text x="250" y="339" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11.5" font-weight="bold" text-anchor="middle">1. move_to(room)</text>
    <text x="250" y="475" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">物理移動 (コスト2〜6)</text>
  </g>

  <!-- 2. CYBER MODE CONTAINER -->
  <g filter="url(#shadow)">
    <rect x="560" y="130" width="380" height="500" rx="16" fill="url(#box-bg)" stroke="url(#teal-grad)" stroke-width="2"/>
    <!-- Container Title -->
    <text x="590" y="175" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="20" font-weight="bold">電脳体モード (Cyber Mode)</text>
    <text x="590" y="202" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13">デバイス等に意識を投射している状態</text>
    
    <!-- State Var -->
    <rect x="590" y="225" width="320" height="40" rx="6" fill="rgba(0, 0, 0, 0.3)" stroke="rgba(45, 212, 191, 0.3)" stroke-width="1"/>
    <text x="605" y="250" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold">位置:</text>
    <text x="645" y="250" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold">projected_room = &quot;living_room&quot;</text>
    
    <!-- Entity A Node -->
    <rect x="590" y="370" width="130" height="90" rx="10" fill="rgba(20, 21, 26, 0.8)" stroke="#0D9488" stroke-width="1.5"/>
    <text x="655" y="405" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">同室エンティティ A</text>
    <text x="655" y="430" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">camera.study</text>
    
    <!-- Entity B Node -->
    <rect x="780" y="370" width="130" height="90" rx="10" fill="rgba(20, 21, 26, 0.8)" stroke="#0D9488" stroke-width="1.5"/>
    <text x="845" y="405" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">別エンティティ B</text>
    <text x="845" y="430" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">external://astrolabe</text>
    
    <!-- Action 3: move_cyber(entity) -->
    <!-- Arrow Right -->
    <path d="M 720 395 Q 750 385 780 395" fill="none" stroke="url(#teal-grad)" stroke-width="2" marker-end="url(#arrow-teal)"/>
    <!-- Arrow Left -->
    <path d="M 780 415 Q 750 425 720 415" fill="none" stroke="url(#teal-grad)" stroke-width="2" marker-end="url(#arrow-teal)"/>
    
    <!-- Background for action text to prevent overlapping issues -->
    <rect x="700" y="325" width="100" height="20" rx="4" fill="rgba(10, 10, 15, 0.8)"/>
    <text x="750" y="339" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" font-weight="bold" text-anchor="middle">3. move_cyber</text>
    <text x="750" y="475" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">電脳内移動 (コスト0)</text>
  </g>

  <!-- 3. TRANSITION ARROWS (BETWEEN CONTAINERS) -->
  
  <!-- Action 2: enter_cyberspace(entity) -->
  <g>
    <!-- Path from Room B (380, 360) to Entity A (610, 360) -->
    <path d="M 380 360 C 430 270, 560 270, 610 360" fill="none" stroke="url(#gold-teal-grad)" stroke-width="3" marker-end="url(#arrow-teal)"/>
    
    <!-- Background for Label -->
    <rect x="330" y="250" width="240" height="50" rx="6" fill="rgba(10, 10, 15, 0.95)" stroke="#2DD4BF" stroke-width="1.5"/>
    <text x="450" y="272" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">2. enter_cyberspace(entity)</text>
    <text x="450" y="290" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">物理体と同室のみ侵入可 (コスト0.35)</text>
    
    <!-- Info Tag shifted near Room B's upper-right -->
    <rect x="310" y="315" width="105" height="20" rx="3" fill="#D4A017"/>
    <text x="362.5" y="329" fill="#000000" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="10.5" font-weight="bold" text-anchor="middle">物理体と同室のみ</text>
  </g>

  <!-- Action 4: return_to_body -->
  <g>
    <!-- Path from Entity A (610, 470) to Room B (380, 470) -->
    <path d="M 610 470 C 560 560, 430 560, 380 470" fill="none" stroke="url(#teal-gold-grad)" stroke-width="3" marker-end="url(#arrow-gold)"/>
    
    <!-- Background for Label -->
    <rect x="330" y="525" width="240" height="50" rx="6" fill="rgba(10, 10, 15, 0.95)" stroke="#D4A017" stroke-width="1.5"/>
    <text x="450" y="547" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">4. return_to_body</text>
    <text x="450" y="565" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">いつでも帰還可能 (コスト0.05)</text>
  </g>

</svg>
"""

def create_cost_model_svg():
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1100" height="700" viewBox="0 0 1100 700">
  <defs>
    <!-- Background Gradient -->
    <linearGradient id="bg-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#14151a"/>
      <stop offset="100%" stop-color="#0a0a0d"/>
    </linearGradient>
    
    <!-- Gold Gradient -->
    <linearGradient id="gold-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#B8960C"/>
    </linearGradient>
    
    <!-- Teal Gradient -->
    <linearGradient id="teal-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2DD4BF"/>
      <stop offset="100%" stop-color="#0D9488"/>
    </linearGradient>

    <!-- Hybrid Gold-Teal Gradient -->
    <linearGradient id="hybrid-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#0D9488"/>
    </linearGradient>
    
    <!-- Card Background -->
    <linearGradient id="card-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(30, 31, 38, 0.4)"/>
      <stop offset="100%" stop-color="rgba(15, 15, 20, 0.6)"/>
    </linearGradient>
    
    <!-- Shadow Filter -->
    <filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="8" stdDeviation="6" flood-color="#000000" flood-opacity="0.6"/>
    </filter>
  </defs>

  <!-- Background -->
  <rect width="1100" height="700" fill="url(#bg-grad)"/>

  <!-- Title -->
  <text x="50" y="60" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="26" font-weight="bold">電脳体モデル コスト対比表</text>
  <text x="50" y="85" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="14">3つのアクションモードによるコストと、精神状態 (body_state) への影響の対比</text>

  <!-- CARD 1: 物理移動 (physical_move) -->
  <g filter="url(#shadow)" transform="translate(80, 130)">
    <rect width="280" height="500" rx="16" fill="url(#card-bg)" stroke="url(#gold-grad)" stroke-width="2"/>
    
    <!-- Icon Placeholder Background -->
    <circle cx="140" cy="70" r="35" fill="rgba(212, 160, 23, 0.1)" stroke="#D4A017" stroke-width="1"/>
    <!-- Icon: Walking Man (Mathematical positioning optimized) -->
    <path d="M136.5 52.5c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2zM132.8 55.9l-2.8 12.6c-.1.5.3.9.8.9.4 0 .7-.3.8-.7l2.2-8.4 2.1 2v5.7c0 .6.4 1 1 1s1-.4 1-1v-6.6c0-.3-.1-.5-.3-.7l-1.9-1.9.6-3.1 2.1 2.1c.2.2.5.3.8.3H142c.6 0 1-.4 1-1s-.4-1-1-1h-2.7l-2.2-2.2c-.4-.4-1-.6-1.5-.6h-2.8c-.8 0-1.5.5-1.8 1.2l-1.9 3.7c-.2.5.1 1.1.7 1.3.5.2 1.1-.1 1.3-.7l1.5-3.7z" 
          fill="#D4A017" transform="translate(-77.6, -22.8) scale(1.6)"/>
          
    <!-- Headers -->
    <text x="140" y="145" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="20" font-weight="bold" text-anchor="middle">物理移動</text>
    <text x="140" y="170" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">physical_move</text>
    
    <!-- Divider -->
    <line x1="40" y1="190" x2="240" y2="190" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>
    
    <!-- Cost Info -->
    <text x="140" y="220" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">アクションコスト</text>
    <text x="140" y="260" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="34" font-weight="bold" text-anchor="middle">2 〜 6</text>
    <text x="140" y="282" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">(部屋グラフ上のパスコスト)</text>
    
    <!-- Divider -->
    <line x1="40" y1="305" x2="240" y2="305" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>

    <!-- body_state effect -->
    <text x="140" y="335" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">body_state 効果</text>
    <text x="140" y="365" fill="#10B981" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="18" font-weight="bold" text-anchor="middle">stress ↓↓  tension ↓↓</text>
    
    <!-- Description -->
    <text x="140" y="405" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">【グラウンディング】</text>
    <text x="140" y="425" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">物理的移動は実体への感覚を</text>
    <text x="140" y="443" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">回復させ、ストレスを解消する</text>

    <!-- Gauge -->
    <g transform="translate(40, 465)">
      <rect width="200" height="8" rx="4" fill="rgba(255, 255, 255, 0.1)"/>
      <rect width="60" height="8" rx="4" fill="#10B981"/>
      <text x="100" y="24" fill="#10B981" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" font-weight="bold" text-anchor="middle">安定・リラックス (↓↓)</text>
    </g>
  </g>

  <!-- CARD 2: 電脳侵入 (remote_avatar) -->
  <g filter="url(#shadow)" transform="translate(410, 130)">
    <rect width="280" height="500" rx="16" fill="url(#card-bg)" stroke="url(#teal-grad)" stroke-width="2"/>
    
    <!-- Icon Placeholder Background -->
    <circle cx="140" cy="70" r="35" fill="rgba(45, 212, 191, 0.1)" stroke="#2DD4BF" stroke-width="1"/>
    <!-- Icon: Lightning (Bolt) -->
    <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" 
          fill="#2DD4BF" transform="translate(118.4, 48.4) scale(1.8)"/>
          
    <!-- Headers -->
    <text x="140" y="145" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="20" font-weight="bold" text-anchor="middle">電脳侵入・移動</text>
    <text x="140" y="170" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">remote_avatar</text>
    
    <!-- Divider -->
    <line x1="40" y1="190" x2="240" y2="190" stroke="rgba(45, 212, 191, 0.2)" stroke-width="1"/>
    
    <!-- Cost Info -->
    <text x="140" y="220" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">アクションコスト</text>
    <text x="140" y="260" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="34" font-weight="bold" text-anchor="middle">0.35 / 0.0</text>
    <text x="140" y="282" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">(侵入時 0.35 / 電脳内移動 0.0)</text>
    
    <!-- Divider -->
    <line x1="40" y1="305" x2="240" y2="305" stroke="rgba(45, 212, 191, 0.2)" stroke-width="1"/>

    <!-- body_state effect -->
    <text x="140" y="335" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">body_state 効果</text>
    <text x="140" y="365" fill="#EF4444" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="18" font-weight="bold" text-anchor="middle">stress ↑  tension ↑</text>
    
    <!-- Description -->
    <text x="140" y="405" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">【不安定化・負荷】</text>
    <text x="140" y="425" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">電脳体投射は精神に負荷を</text>
    <text x="140" y="443" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">与える (物理体から遠いほど増大)</text>

    <!-- Gauge -->
    <g transform="translate(40, 465)">
      <rect width="200" height="8" rx="4" fill="rgba(255, 255, 255, 0.1)"/>
      <rect width="150" height="8" rx="4" fill="#EF4444"/>
      <text x="100" y="24" fill="#EF4444" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" font-weight="bold" text-anchor="middle">精神負荷・不安定化 (↑)</text>
    </g>
  </g>

  <!-- CARD 3: 投射先での操作 (cyber_in_room) -->
  <g filter="url(#shadow)" transform="translate(740, 130)">
    <rect width="280" height="500" rx="16" fill="url(#card-bg)" stroke="url(#hybrid-grad)" stroke-width="2"/>
    
    <!-- Icon Placeholder Background -->
    <circle cx="140" cy="70" r="35" fill="rgba(45, 212, 191, 0.1)" stroke="url(#hybrid-grad)" stroke-width="1"/>
    <!-- Icon: Cursor (MousePointer) -->
    <path d="M3 3l7.07 16.97 2.51-7.39 7.39-2.51L3 3z" 
          fill="#2DD4BF" transform="translate(124, 54) scale(1.6)"/>
    <circle cx="152" cy="70" r="8" fill="none" stroke="#D4A017" stroke-width="1.5" stroke-dasharray="3 2"/>
          
    <!-- Headers -->
    <text x="140" y="145" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="20" font-weight="bold" text-anchor="middle">投射先での操作</text>
    <text x="140" y="170" fill="url(#hybrid-grad)" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">cyber_in_room</text>
    
    <!-- Divider -->
    <line x1="40" y1="190" x2="240" y2="190" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>
    
    <!-- Cost Info -->
    <text x="140" y="220" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">アクションコスト</text>
    <text x="140" y="260" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="34" font-weight="bold" text-anchor="middle">0.05</text>
    <text x="140" y="282" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">(物理同室でのデバイス直接操作と同等)</text>
    
    <!-- Divider -->
    <line x1="40" y1="305" x2="240" y2="305" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>

    <!-- body_state effect -->
    <text x="140" y="335" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">body_state 効果</text>
    <text x="140" y="365" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="18" font-weight="bold" text-anchor="middle">— (中立)</text>
    
    <!-- Description -->
    <text x="140" y="405" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" font-weight="bold" text-anchor="middle">【中立・安定】</text>
    <text x="140" y="425" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">投射先デバイスの操作自体は</text>
    <text x="140" y="443" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13" text-anchor="middle">精神状態に影響を与えない</text>

    <!-- Gauge -->
    <g transform="translate(40, 465)">
      <rect width="200" height="8" rx="4" fill="rgba(255, 255, 255, 0.1)"/>
      <rect width="100" height="8" rx="4" fill="#9CA3AF"/>
      <text x="100" y="24" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" font-weight="bold" text-anchor="middle">精神変化なし (—)</text>
    </g>
  </g>

</svg>
"""

def create_curiosity_cycle_svg():
    return """<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="750" viewBox="0 0 1000 750">
  <defs>
    <!-- Background Gradient -->
    <linearGradient id="bg-grad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#14151a"/>
      <stop offset="100%" stop-color="#0a0a0d"/>
    </linearGradient>
    
    <!-- Gold Gradient -->
    <linearGradient id="gold-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#B8960C"/>
    </linearGradient>
    
    <!-- Teal Gradient -->
    <linearGradient id="teal-grad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2DD4BF"/>
      <stop offset="100%" stop-color="#0D9488"/>
    </linearGradient>

    <!-- Gold-Teal Gradient (Circular path 1) -->
    <linearGradient id="arrow-grad-1" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D4A017"/>
      <stop offset="100%" stop-color="#2DD4BF"/>
    </linearGradient>
    
    <!-- Teal-Gold Gradient (Circular path 2) -->
    <linearGradient id="arrow-grad-2" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0%" stop-color="#2DD4BF"/>
      <stop offset="100%" stop-color="#D4A017"/>
    </linearGradient>
    
    <!-- Box Background -->
    <linearGradient id="box-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(30, 31, 38, 0.4)"/>
      <stop offset="100%" stop-color="rgba(15, 15, 20, 0.6)"/>
    </linearGradient>
    
    <!-- Warning Box Background -->
    <linearGradient id="warn-bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="rgba(239, 68, 68, 0.08)"/>
      <stop offset="100%" stop-color="rgba(220, 38, 38, 0.15)"/>
    </linearGradient>
    
    <!-- Shadow Filter -->
    <filter id="shadow" x="-10%" y="-10%" width="120%" height="120%">
      <feDropShadow dx="0" dy="8" stdDeviation="6" flood-color="#000000" flood-opacity="0.6"/>
    </filter>

    <!-- Arrow Markers -->
    <marker id="arrow-gold" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#D4A017"/>
    </marker>
    <marker id="arrow-teal" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#2DD4BF"/>
    </marker>
  </defs>

  <!-- Background -->
  <rect width="1000" height="750" fill="url(#bg-grad)"/>

  <!-- Title -->
  <text x="50" y="60" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="26" font-weight="bold">好奇心 (Curiosity) 探索サイクル</text>
  <text x="50" y="85" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="14">好奇心が電脳探索の負荷を緩和し、自動回復と逆圧力によってループするメカニズム</text>

  <!-- CIRCLE GRADIENT ARROWS (CYCLE) - Optimized so they do not overlap boxes -->
  <!-- Arc 1: High Curiosity -> Exploration (Top to Right-Down) -->
  <path d="M 600 250 C 680 270, 800 280, 800 320" fill="none" stroke="url(#arrow-grad-1)" stroke-width="4" marker-end="url(#arrow-teal)"/>
  <text x="715" y="275" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" font-weight="bold">探索開始 (負荷緩和)</text>

  <!-- Arc 2: Exploration -> Return/Rest (Right-Down to Left-Down) -->
  <path d="M 700 470 C 600 530, 400 530, 300 470" fill="none" stroke="url(#arrow-grad-2)" stroke-width="4" stroke-dasharray="8 4" marker-end="url(#arrow-gold)"/>
  <text x="500" y="525" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">好奇心低下で帰還圧上昇</text>

  <!-- Arc 3: Return/Rest -> High Curiosity (Left-Down to Top) -->
  <path d="M 200 320 C 200 280, 320 270, 400 250" fill="none" stroke="url(#gold-grad)" stroke-width="4" marker-end="url(#arrow-gold)"/>
  <text x="285" y="275" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" font-weight="bold" text-anchor="end">自動回復 (チャージ)</text>


  <!-- NODE 1: 好奇心が高い (Top) -->
  <g filter="url(#shadow)" transform="translate(330, 130)">
    <rect width="340" height="120" rx="12" fill="url(#box-bg)" stroke="url(#gold-grad)" stroke-width="2"/>
    <text x="170" y="35" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="16" font-weight="bold" text-anchor="middle">1. 好奇心が高い (curiosity ↑)</text>
    <line x1="30" y1="48" x2="310" y2="48" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>
    <text x="170" y="70" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">電脳移動のストレス負荷が緩和される</text>
    <text x="170" y="90" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" font-weight="bold" text-anchor="middle">curiosity_factor = max(0.2, 1.0 - curiosity)</text>
    <text x="170" y="105" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="10" text-anchor="middle">※curiosity=0.9 で負荷が通常の 20% に激減</text>
  </g>

  <!-- NODE 2: 電脳空間を探索 (Right-Down) -->
  <g filter="url(#shadow)" transform="translate(660, 320)">
    <rect width="280" height="140" rx="12" fill="url(#box-bg)" stroke="url(#teal-grad)" stroke-width="2"/>
    <text x="140" y="35" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="15" font-weight="bold" text-anchor="middle">2. 電脳空間を探索する</text>
    <line x1="20" y1="48" x2="260" y2="48" stroke="rgba(45, 212, 191, 0.2)" stroke-width="1"/>
    <text x="140" y="70" fill="#2DD4BF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">アクション実行で好奇心が僅かに消費</text>
    <!-- Consumption Values -->
    <text x="140" y="98" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11.5" text-anchor="middle">電脳侵入時: -0.015 / アクション</text>
    <text x="140" y="116" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11.5" text-anchor="middle">電脳内移動: -0.008 / アクション</text>
  </g>

  <!-- NODE 3: 物理体に帰還・休息 (Left-Down) -->
  <g filter="url(#shadow)" transform="translate(60, 320)">
    <rect width="280" height="140" rx="12" fill="url(#box-bg)" stroke="url(#gold-grad)" stroke-width="2"/>
    <text x="140" y="35" fill="#FFFFFF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="15" font-weight="bold" text-anchor="middle">3. 物理体に帰還・休息</text>
    <line x1="20" y1="48" x2="260" y2="48" stroke="rgba(212, 160, 23, 0.2)" stroke-width="1"/>
    <text x="140" y="70" fill="#D4A017" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="12" text-anchor="middle">物理体で過ごす時間で好奇心が自動回復</text>
    <!-- Recovery values -->
    <text x="140" y="98" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11.5" font-weight="bold" text-anchor="middle">advance_tick で自動回復 (+0.01 / tick)</text>
    <text x="140" y="116" fill="#9CA3AF" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="10.5" text-anchor="middle">物理体移動 (physical_move) は stress を大幅解消</text>
  </g>

  <!-- WARNING BOX (Low curiosity, reverse drift pressure) -->
  <g filter="url(#shadow)" transform="translate(350, 290)">
    <rect width="300" height="120" rx="12" fill="url(#warn-bg)" stroke="#EF4444" stroke-width="1.5"/>
    
    <!-- Warning Icon -->
    <g transform="translate(136, 12)">
      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z M12 9v4 M12 17h.01" 
            stroke="#EF4444" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
    </g>

    <!-- Warning Text -->
    <text x="150" y="55" fill="#EF4444" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="13.5" font-weight="bold" text-anchor="middle">低好奇心による帰還への逆圧力</text>
    <text x="150" y="78" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">好奇心が低下すると電脳空間での時間のズレ</text>
    <text x="150" y="94" fill="#E5E7EB" font-family="Noto Sans CJK JP, DejaVu Sans, sans-serif" font-size="11" text-anchor="middle">(電脳ドリフト) が加速し、帰還圧が上昇する</text>
  </g>

</svg>
"""

def generate_diagrams():
    docs_dir = "/config/GitHub/embodied-ha/docs/"
    os.makedirs(docs_dir, exist_ok=True)
    
    # 1. Action Flow Diagram
    action_flow_svg = create_action_flow_svg()
    action_flow_png_path = os.path.join(docs_dir, "action_flow.png")
    cairosvg.svg2png(bytestring=action_flow_svg.encode('utf-8'), write_to=action_flow_png_path)
    print(f"Generated: {action_flow_png_path}")
    
    # 2. Cost Model Diagram
    cost_model_svg = create_cost_model_svg()
    cost_model_png_path = os.path.join(docs_dir, "cost_model.png")
    cairosvg.svg2png(bytestring=cost_model_svg.encode('utf-8'), write_to=cost_model_png_path)
    print(f"Generated: {cost_model_png_path}")
    
    # 3. Curiosity Cycle Diagram
    curiosity_cycle_svg = create_curiosity_cycle_svg()
    curiosity_cycle_png_path = os.path.join(docs_dir, "curiosity_cycle.png")
    cairosvg.svg2png(bytestring=curiosity_cycle_svg.encode('utf-8'), write_to=curiosity_cycle_png_path)
    print(f"Generated: {curiosity_cycle_png_path}")

if __name__ == "__main__":
    generate_diagrams()
