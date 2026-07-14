# Mineradio UI 设计规范 / Design System

> 从 Mineradio v1.1.1 源码提取，可作为同类沉浸式深色应用的 UI 范本

---

## 一、设计风格定义

**关键词：** 黑色玻璃舞台 / 沉浸式 / 模糊磨砂 / 琥珀金点缀 / 粒子氛围

整体是一个深色"舞台"空间，背景趋近纯黑，所有 UI 元件像悬浮在玻璃面板上，用 `backdrop-filter: blur` 和半透明背景制造层次感，用琥珀金色（`#f4d28a`）作为唯一强调色。没有白色区域，没有强对比边框，所有东西都"沉"在黑暗里。

---

## 二、颜色系统

### 背景层

```css
/* 舞台底层（最深） */
--bg-stage:        #000000;           /* 纯黑，视觉背景 */
--bg-stage-soft:   rgba(10, 10, 14, 0.93);  /* 略带蓝调的深黑 */

/* 浮层/面板背景（玻璃效果） */
--bg-panel:        rgba(20, 20, 28, 0.95);  /* 深蓝黑，主面板 */
--bg-panel-light:  rgba(24, 23, 26, 0.96);  /* 略浅，tooltip 等 */

/* 悬停/激活状态 */
--bg-hover:        rgba(255, 255, 255, 0.08);
--bg-active:       rgba(255, 255, 255, 0.12);
```

### 边框层

```css
/* 金色描边（强调/选中） */
--border-gold:         rgba(244, 210, 138, 0.24);   /* 常规金边 */
--border-gold-light:   rgba(244, 210, 138, 0.22);   /* tooltip 箭头 */

/* 白色描边（通用分割线） */
--border-subtle:       rgba(255, 255, 255, 0.035);  /* 极淡，几乎不可见 */
--border-default:      rgba(255, 255, 255, 0.12);   /* toast / 普通面板 */
--border-strong:       rgba(255, 255, 255, 0.07);   /* inset 高光线 */
```

### 文字层

```css
--text-primary:        #ffffff;                     /* 纯白，标题/重要内容 */
--text-secondary:      rgba(255, 255, 255, 0.70);   /* 70% 白，正文 */
--text-muted:          rgba(255, 255, 255, 0.38);   /* 38% 白，次要信息 */
--text-gold:           #fff0bf;                     /* 琥珀白，高亮标题 */

/* 强调色 */
--accent-gold:         #f4d28a;                     /* 主强调色，琥珀金 */
--accent-teal:         #00f5d4;                     /* 次强调色，UI 高亮 */
--accent-blue:         #7fd8ff;                     /* 视觉图标色 */
```

### 阴影层

```css
--shadow-panel:    0 20px 60px rgba(0, 0, 0, 0.44);
--shadow-float:    0 0 0 1px rgba(255, 255, 255, 0.035);
--shadow-inset:    inset 0 1px 0 rgba(255, 255, 255, 0.07);
```

---

## 三、毛玻璃面板（核心组件）

Mineradio 所有浮层都遵循同一套玻璃面板公式：

```css
.glass-panel {
  background: linear-gradient(
    180deg,
    rgba(24, 23, 26, 0.96),
    rgba(10, 10, 14, 0.93)
  );
  border: 1px solid rgba(244, 210, 138, 0.24);   /* 金色边框 */
  border-radius: 12px;
  box-shadow:
    0 20px 60px rgba(0, 0, 0, 0.44),             /* 深阴影 */
    0 0 0 1px rgba(255, 255, 255, 0.035),         /* 外描边 */
    inset 0 1px 0 rgba(255, 255, 255, 0.07);      /* 顶部高光 */
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
}
```

### 普通面板（无金边）

```css
.glass-panel-plain {
  background: rgba(20, 20, 28, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 10px;
  backdrop-filter: blur(20px);
}
```

---

## 四、Toast 通知

从源码第 981 行提取：

```css
#toast {
  position: fixed;
  z-index: 200;
  top: 84px;
  left: 50%;
  transform: translateX(-50%) translateY(-8px);

  /* 玻璃风格 */
  background: rgba(20, 20, 28, 0.95);
  border: 1px solid rgba(255, 255, 255, 0.12);
  backdrop-filter: blur(20px);

  /* 文字 */
  color: #fff;
  font-size: 12.5px;
  letter-spacing: 0.5px;
  padding: 10px 18px;
  border-radius: 10px;

  /* 动画 */
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.3s, transform 0.3s;
}

#toast.show {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}
```

**JS 调用方式：**

```js
var toastTimer = null;

function showToast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function() {
    t.classList.remove('show');
  }, 2600);  // 2.6 秒后消失
}
```

**HTML：**

```html
<div id="toast"></div>
```

---

## 五、Tooltip / 气泡提示

从源码第 352 行提取（`#upload-tip`）：

```css
.tooltip-bubble {
  position: absolute;
  z-index: 3;
  width: 214px;
  padding: 13px 34px 13px 14px;
  border-radius: 12px;

  /* 玻璃面板（金边款） */
  background: linear-gradient(
    180deg,
    rgba(24, 23, 26, 0.96),
    rgba(10, 10, 14, 0.93)
  );
  border: 1px solid rgba(244, 210, 138, 0.24);
  box-shadow:
    0 20px 60px rgba(0, 0, 0, 0.44),
    0 0 0 1px rgba(255, 255, 255, 0.035),
    inset 0 1px 0 rgba(255, 255, 255, 0.07);

  /* 文字 */
  color: rgba(255, 255, 255, 0.70);
  font-size: 12px;
  line-height: 1.5;
  letter-spacing: 0.25px;

  /* 动画 */
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transform: translateY(-8px) scale(0.98);
  transition: opacity 0.3s, transform 0.3s, visibility 0.3s;
  will-change: opacity, transform;
}

.tooltip-bubble.show {
  opacity: 1;
  visibility: visible;
  pointer-events: auto;
  transform: translateY(0) scale(1);
}

/* 上方小箭头 */
.tooltip-bubble::before {
  content: "";
  position: absolute;
  left: 18px;
  top: -6px;
  width: 10px;
  height: 10px;
  transform: rotate(45deg);
  background: rgba(24, 23, 26, 0.96);
  border-left: 1px solid rgba(244, 210, 138, 0.22);
  border-top: 1px solid rgba(244, 210, 138, 0.22);
}

/* tooltip 内标题 */
.tooltip-title {
  display: block;
  color: #fff0bf;       /* 琥珀白 */
  font-weight: 700;
  margin-bottom: 2px;
}

/* 关闭按钮 */
.tooltip-close {
  position: absolute;
  right: 9px;
  top: 8px;
  width: 18px;
  height: 18px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: rgba(255, 255, 255, 0.38);
  cursor: pointer;
  font-size: 15px;
  line-height: 18px;
  padding: 0;
}

.tooltip-close:hover {
  background: rgba(255, 255, 255, 0.08);
  color: #fff;
}
```

---

## 六、字体排版

```css
/* 字体族 */
font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;

/* 字号体系 */
--font-xs:   11px;   /* 极小标注 */
--font-sm:   12px;   /* tooltip 正文、次要标注 */
--font-base: 12.5px; /* toast、主 UI 文字 */
--font-md:   13px;   /* 正文 */
--font-lg:   15px;   /* 小标题 */

/* 字间距 */
--tracking-tight:  0.25px;  /* tooltip 正文 */
--tracking-normal: 0.5px;   /* toast */

/* 行高 */
--leading-normal: 1.5;
```

---

## 七、圆角体系

```css
--radius-sm:   8px;   /* 小按钮、badge */
--radius-md:   10px;  /* toast、小面板 */
--radius-lg:   12px;  /* 主面板、tooltip、卡片 */
--radius-full: 50%;   /* 圆形按钮、头像 */
```

---

## 八、动画体系

```css
/* 标准过渡 */
--transition-base: opacity 0.3s, transform 0.3s;

/* 入场状态（隐藏） */
.enter-hidden {
  opacity: 0;
  transform: translateY(-8px) scale(0.98);
}

/* 入场状态（显示） */
.enter-shown {
  opacity: 1;
  transform: translateY(0) scale(1);
}

/* 背景模糊 */
backdrop-filter: blur(20px);
will-change: opacity, transform, filter;
```

---

## 九、层级体系（z-index）

```
z-index: 3     → tooltip / 气泡提示
z-index: 200   → toast 通知
z-index: 9999  → 最高层（全屏遮罩、模态框）
```

---

## 十、快速复用模板

### 最小化深色应用 HTML 骨架

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: #000;
      color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 13px;
      min-height: 100vh;
    }

    /* 毛玻璃面板 */
    .glass {
      background: linear-gradient(180deg, rgba(24,23,26,.96), rgba(10,10,14,.93));
      border: 1px solid rgba(244,210,138,.24);
      border-radius: 12px;
      box-shadow: 0 20px 60px rgba(0,0,0,.44),
                  0 0 0 1px rgba(255,255,255,.035),
                  inset 0 1px 0 rgba(255,255,255,.07);
      backdrop-filter: blur(20px);
    }

    /* Toast */
    #toast {
      position: fixed;
      z-index: 200;
      top: 84px;
      left: 50%;
      transform: translateX(-50%) translateY(-8px);
      background: rgba(20,20,28,.95);
      border: 1px solid rgba(255,255,255,.12);
      color: #fff;
      padding: 10px 18px;
      border-radius: 10px;
      font-size: 12.5px;
      letter-spacing: .5px;
      opacity: 0;
      pointer-events: none;
      transition: opacity .3s, transform .3s;
      backdrop-filter: blur(20px);
    }
    #toast.show {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }

    /* 按钮 */
    .btn {
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.12);
      border-radius: 8px;
      color: rgba(255,255,255,.70);
      cursor: pointer;
      font-size: 12px;
      letter-spacing: .25px;
      padding: 7px 14px;
      transition: background .2s, color .2s;
    }
    .btn:hover {
      background: rgba(255,255,255,.14);
      color: #fff;
    }
    .btn-gold {
      border-color: rgba(244,210,138,.4);
      color: #f4d28a;
    }
    .btn-gold:hover {
      background: rgba(244,210,138,.1);
    }
  </style>
</head>
<body>

  <div id="toast"></div>

  <script>
    var toastTimer = null;
    function showToast(msg) {
      var t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function(){ t.classList.remove('show'); }, 2600);
    }
  </script>
</body>
</html>
```

---

## 十一、设计原则总结

| 原则 | 做法 |
|------|------|
| 背景永远深黑 | `#000` 或 `rgba(10,10,14,x)` |
| 面板用毛玻璃 | `backdrop-filter: blur(20px)` + 半透明深色背景 |
| 边框极淡 | `rgba(255,255,255,0.12)` 或更低 |
| 强调色唯一 | 琥珀金 `#f4d28a`，只用在最重要的元素 |
| 文字分三层 | 纯白 / 70%白 / 38%白 |
| 动画克制 | 只有 `opacity` 和 `translateY`，无弹跳 |
| 圆角统一 | 12px 主面板，10px 小元件，8px 按钮 |
