// 后台前端小脚本

// 手机号即时过滤：输入即筛表格行，不刷新页面（tr 需带 data-phone）
function filterByPhone(input) {
  const q = input.value.trim();
  document.querySelectorAll("tr[data-phone]").forEach(tr => {
    tr.style.display = (!q || tr.dataset.phone.includes(q)) ? "" : "none";
  });
}

// 敏感操作验证码（二级账号）——发到一级管理员手机，开发模式显示
function sendVcode(shopId, hintId) {
  fetch(`/s/${shopId}/admin/api/send_vcode`, { method: "POST" })
    .then(r => r.json()).then(d => {
      if (!d.ok) { alert(d.msg); return; }
      if (d.dev_code) {
        const h = document.getElementById(hintId);
        if (h) h.textContent = "（开发模式）验证码：" + d.dev_code;
      }
    });
}
