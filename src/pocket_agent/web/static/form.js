/* Intake form — POST to /api/intake */
(function () {
  'use strict';

  var form = document.getElementById('intake-form');
  var status = document.getElementById('form-status');

  if (!form) return;

  form.addEventListener('submit', function (e) {
    e.preventDefault();

    var btn = form.querySelector('button[type="submit"]');
    btn.disabled = true;
    status.textContent = '';
    status.className = 'form-status';

    var data = {
      name: form.name.value.trim(),
      email: form.email.value.trim(),
      project: form.project.value.trim(),
      budget: form.budget.value,
      timeline: form.timeline.value,
    };

    fetch('/api/intake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    })
      .then(function (res) {
        return res.json().then(function (body) {
          if (!res.ok) throw new Error(body.error || 'Submission failed');
          status.textContent = body.message || 'Sent!  We\'ll be in touch.';
          status.className = 'form-status success';
          form.reset();
        });
      })
      .catch(function (err) {
        status.textContent = err.message || 'Something went wrong.  Please try again.';
        status.className = 'form-status error';
      })
      .finally(function () {
        btn.disabled = false;
      });
  });
})();
