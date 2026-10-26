/* ===================================================================
   nusoft Landing Page Interactive Logic
   Theme, Simulator, Tabs, Accordion, Modal & Animation
   =================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initNavbarScroll();
  initSimulator();
  initFeatureTabs();
  initFaqAccordion();
  initModal();
});

/* -------------------------------------------------------------
   1. Theme Toggle (Dark / Light)
   ------------------------------------------------------------- */
function initTheme() {
  const toggleBtn = document.getElementById('themeToggleBtn');
  const themeIcon = document.getElementById('themeIcon');
  const savedTheme = localStorage.getItem('nusoft-theme') || 'dark';

  document.documentElement.setAttribute('data-theme', savedTheme);
  updateThemeIcon(savedTheme, themeIcon);

  if (toggleBtn) {
    toggleBtn.addEventListener('click', () => {
      const current = document.documentElement.getAttribute('data-theme');
      const next = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      localStorage.setItem('nusoft-theme', next);
      updateThemeIcon(next, themeIcon);
    });
  }
}

function updateThemeIcon(theme, iconEl) {
  if (!iconEl) return;
  if (theme === 'dark') {
    iconEl.innerHTML = `
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="12" cy="12" r="5"></circle>
        <line x1="12" y1="1" x2="12" y2="3"></line>
        <line x1="12" y1="21" x2="12" y2="23"></line>
        <line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line>
        <line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line>
        <line x1="1" y1="12" x2="3" y2="12"></line>
        <line x1="21" y1="12" x2="23" y2="12"></line>
        <line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line>
        <line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line>
      </svg>
    `;
  } else {
    iconEl.innerHTML = `
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path>
      </svg>
    `;
  }
}

/* -------------------------------------------------------------
   2. Navbar Scroll Glass Effect
   ------------------------------------------------------------- */
function initNavbarScroll() {
  const navbar = document.getElementById('navbar');
  window.addEventListener('scroll', () => {
    if (window.scrollY > 40) {
      navbar.classList.add('scrolled');
    } else {
      navbar.classList.remove('scrolled');
    }
  });
}

/* -------------------------------------------------------------
   3. NuShift Real-time Staffing Simulator
   ------------------------------------------------------------- */
function initSimulator() {
  const s0 = document.getElementById('simAge0');
  const s1 = document.getElementById('simAge1');
  const s3 = document.getElementById('simAge3');
  const s4 = document.getElementById('simAge4');
  const ruleSelect = document.getElementById('simRuleSelect');

  const v0 = document.getElementById('valAge0');
  const v1 = document.getElementById('valAge1');
  const v3 = document.getElementById('valAge3');
  const v4 = document.getElementById('valAge4');

  const totalStaffEl = document.getElementById('simTotalStaff');
  const totalChildrenEl = document.getElementById('simTotalChildren');

  const b0 = document.getElementById('bdAge0');
  const b1 = document.getElementById('bdAge1');
  const b3 = document.getElementById('bdAge3');
  const b4 = document.getElementById('bdAge4');

  if (!s0 || !s1 || !s3 || !s4) return;

  function recalculate() {
    const n0 = parseInt(s0.value, 10);
    const n1 = parseInt(s1.value, 10);
    const n3 = parseInt(s3.value, 10);
    const n4 = parseInt(s4.value, 10);

    v0.textContent = `${n0}名`;
    v1.textContent = `${n1}名`;
    v3.textContent = `${n3}名`;
    v4.textContent = `${n4}名`;

    const rule = ruleSelect.value;
    let r3 = 20;
    let r4 = 30;

    if (rule === 'tokyo') {
      r3 = 15;
      r4 = 25;
    } else if (rule === 'handy') {
      r3 = 15;
      r4 = 20;
    }

    // 厚労省配置基準端数計算 (法的には各クラス小数点を足してから切り上げる自治体とクラス別切り上げがあるが、概算標準算出)
    // 0歳: 3:1, 1・2歳: 6:1, 3歳: 20:1(15:1), 4・5歳: 30:1(25:1)
    const req0 = n0 > 0 ? Math.ceil(n0 / 3) : 0;
    const req1 = n1 > 0 ? Math.ceil(n1 / 6) : 0;
    const req3 = n3 > 0 ? Math.ceil(n3 / r3) : 0;
    const req4 = n4 > 0 ? Math.ceil(n4 / r4) : 0;

    let totalStaff = req0 + req1 + req3 + req4;
    // 乳幼児保育の最低配置基準（開所時は最低保育士2名必須）
    if (totalStaff < 2 && (n0 + n1 + n3 + n4) > 0) {
      totalStaff = 2;
    }

    totalStaffEl.textContent = totalStaff;
    totalChildrenEl.textContent = n0 + n1 + n3 + n4;

    b0.textContent = `${req0}名 (3:1)`;
    b1.textContent = `${req1}名 (6:1)`;
    b3.textContent = `${req3}名 (${r3}:1)`;
    b4.textContent = `${req4}名 (${r4}:1)`;
  }

  [s0, s1, s3, s4].forEach(slider => slider.addEventListener('input', recalculate));
  ruleSelect.addEventListener('change', recalculate);

  recalculate();
}

/* -------------------------------------------------------------
   4. Feature Showcase Tabs
   ------------------------------------------------------------- */
function initFeatureTabs() {
  const tabBtns = document.querySelectorAll('.tab-btn');
  const tabContents = document.querySelectorAll('.tab-content');

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.getAttribute('data-tab');

      tabBtns.forEach(b => b.classList.remove('active'));
      tabContents.forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      const activeContent = document.getElementById(`tab-${target}`);
      if (activeContent) {
        activeContent.classList.add('active');
      }
    });
  });
}

/* -------------------------------------------------------------
   5. FAQ Accordion
   ------------------------------------------------------------- */
function initFaqAccordion() {
  const faqItems = document.querySelectorAll('.faq-item');

  faqItems.forEach(item => {
    const question = item.querySelector('.faq-question');
    const answer = item.querySelector('.faq-answer');

    question.addEventListener('click', () => {
      const isActive = item.classList.contains('active');

      // 他を閉じる
      faqItems.forEach(other => {
        other.classList.remove('active');
        const ans = other.querySelector('.faq-answer');
        if (ans) ans.style.maxHeight = null;
      });

      if (!isActive) {
        item.classList.add('active');
        answer.style.maxHeight = answer.scrollHeight + 30 + 'px';
      }
    });
  });
}

/* -------------------------------------------------------------
   6. Demo & Contact Modal
   ------------------------------------------------------------- */
function initModal() {
  const openBtns = document.querySelectorAll('.open-modal-btn');
  const modal = document.getElementById('demoModal');
  const closeBtn = document.getElementById('closeModalBtn');
  const form = document.getElementById('demoForm');

  if (!modal) return;

  openBtns.forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      modal.classList.add('open');
    });
  });

  if (closeBtn) {
    closeBtn.addEventListener('click', () => {
      modal.classList.remove('open');
    });
  }

  modal.addEventListener('click', (e) => {
    if (e.target === modal) {
      modal.classList.remove('open');
    }
  });

  if (form) {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const submitBtn = form.querySelector('button[type="submit"]');
      const originalText = submitBtn.textContent;
      submitBtn.disabled = true;
      submitBtn.textContent = '送信中...';

      setTimeout(() => {
        form.innerHTML = `
          <div style="text-align: center; padding: 20px 0;">
            <div style="font-size: 3rem; margin-bottom: 12px;">🎉</div>
            <h3 style="font-size: 1.5rem; margin-bottom: 8px;">お申し込みを受け付けました</h3>
            <p style="color: var(--text-secondary); font-size: 0.95rem; margin-bottom: 24px;">
              ご登録いただいたメールアドレス宛に、デモ環境のログイン情報をお送りいたします。
            </p>
            <button type="button" class="btn btn-secondary" onclick="document.getElementById('demoModal').classList.remove('open'); location.reload();">閉じる</button>
          </div>
        `;
      }, 1000);
    });
  }
}
