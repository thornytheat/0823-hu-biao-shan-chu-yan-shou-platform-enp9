import {request} from './client.js';
import {snapshot, history} from './view.js';
import {visualize} from './plots.js';

const form = document.querySelector('#source');
const file = document.querySelector('#file');
const profile = document.querySelector('#profile');
const status = document.querySelector('#notice');
const detail = document.querySelector('#review');
const evidence = document.querySelector('#evidence');
const supplementForm = document.querySelector('#supplement-form');
const supplementKey = document.querySelector('#supplement-key');
const supplementFile = document.querySelector('#supplement-file');
const versionSwitch = document.querySelector('#version-switch');
const versionSummary = document.querySelector('#version-summary');
const supplementMarks = document.querySelector('#supplement-marks');
const stages = [...document.querySelectorAll('[data-stage]')];
let stage = 'balance';
let report, selected, batch, running = false;
let originalBatch = null;
let currentBatch = null;
let viewMode = 'current';

function source() {
  const data = new FormData();
  if (file.files.length) data.append('file',file.files[0]);
  else data.append('sample',profile.value);
  return data;
}

function announce(text, failure = false) {
  status.textContent = text;
  status.dataset.failure = String(failure);
}

function displayedBatch() {
  return viewMode === 'original' && originalBatch ? originalBatch : (currentBatch || batch);
}

function draw() {
  if (!report) return;
  snapshot(report, selected, stage);
  visualize(report, selected);
  for (const button of stages) button.setAttribute('aria-pressed',String(button.dataset.stage===stage));
  document.querySelector('#panel-title').textContent = stages.find(button => button.dataset.stage===stage)?.textContent || '运行对账';
  detail.hidden = !batch;
  if (supplementForm) supplementForm.hidden = !batch;
  if (versionSwitch) versionSwitch.hidden = !originalBatch;
  if (versionSwitch) {
    for (const button of versionSwitch.querySelectorAll('[data-version-view]')) {
      button.setAttribute('aria-pressed', String(button.dataset.versionView === viewMode));
    }
  }
  const shownBatch = displayedBatch();
  if (versionSummary && originalBatch) {
    const visibleTotal = batchView => (batchView?.report?.cases || [])
      .reduce((sum, item) => sum + (item.measure?.value ?? 0), 0);
    const originalTotal = visibleTotal(originalBatch);
    const currentTotal = currentBatch ? visibleTotal(currentBatch) : null;
    versionSwitch.dataset.originalVisibleCount = String(originalTotal);
    if (currentTotal !== null) versionSwitch.dataset.currentVisibleCount = String(currentTotal);
    const originalCount = versionSummary.querySelector('[data-original-visible-count]');
    const currentCount = versionSummary.querySelector('[data-current-visible-count]');
    if (originalCount) originalCount.textContent = `原快照可见：${originalTotal}（${originalBatch.status}）`;
    if (currentCount && currentBatch) currentCount.textContent = `当前可见：${currentTotal}（${currentBatch.status}）`;
    versionSummary.title = currentBatch
      ? `原快照 ${originalBatch.id} · 当前 ${currentBatch.id}`
      : originalBatch.id;
  }
  if (supplementMarks) {
    const marksSource = currentBatch?.report || batch?.report;
    const marks = marksSource?.marked_records || [];
    supplementMarks.dataset.records = marks.join(',');
    supplementMarks.textContent = marks.length
      ? `暂缓签发，已标出原记录：${marks.join('、')}`
      : '';
  }
  if (batch) {
    const releasable = batch.report?.current_balance?.releasable
      ?? batch.report?.balance?.releasable
      ?? false;
    document.querySelector('#run-status').textContent = `当前版本 ${batch.id.slice(0,10)} · ${batch.status} · r${batch.revision}`;
    detail.querySelector('[value="APPROVED"]').disabled = !releasable || batch.status==='APPROVED';
    detail.querySelector('[value="HELD"]').disabled = batch.status==='APPROVED';
    if (evidence && shownBatch) evidence.href = `/api/runs/${shownBatch.id}/evidence`;
  }
}

async function loadLineage(runId) {
  try {
    const lineage = await request(`/api/runs/${runId}/versions`);
    const hasHistory = (lineage.supplements?.length || 0) > 0 || (lineage.versions?.length || 0) > 1;
    if (!hasHistory) {
      originalBatch = null;
      versionSwitch.hidden = true;
      return;
    }
    originalBatch = lineage.original;
    currentBatch = lineage.current;
    if (viewMode === 'original' && originalBatch) report = originalBatch.report;
    draw();
  } catch (error) {
    announce(error.message, true);
  }
}

function show(payload, committed = false) {
  if (payload?.original && payload?.current) {
    originalBatch = payload.original;
    currentBatch = payload.current;
    batch = payload.current;
    viewMode = 'current';
  } else if (committed) {
    batch = payload;
    if (payload?.report?.supplement?.original_run_id) {
      currentBatch = payload;
      const rootId = payload.report.supplement.original_run_id;
      if (!originalBatch || originalBatch.id !== rootId) originalBatch = null;
      if (viewMode !== 'original' || !originalBatch) viewMode = 'current';
    } else {
      originalBatch = null;
      currentBatch = null;
      viewMode = 'current';
    }
    loadLineage(payload.id);
  } else {
    batch = null;
    originalBatch = null;
    currentBatch = null;
    viewMode = 'current';
  }
  const shown = displayedBatch();
  report = viewMode === 'original' && originalBatch ? originalBatch.report : (shown?.report || payload);
  detail.elements.note.value = batch?.note || '';
  selected = report.cases?.[0]?.case;
  draw();
}

async function refresh() {
  const data = await request('/api/runs');
  history(data.items, batch?.id);
}

async function perform(action) {
  if (running) return;
  running = true;
  document.body.setAttribute('aria-busy','true');
  announce('处理中…');
  try { await action(); }
  catch (error) { announce(error.message,true); }
  finally { running = false; document.body.removeAttribute('aria-busy'); }
}

function preview(next) {
  return perform(async () => {
    const payload = await request(`/api/stages/${next}`,{method:'POST',body:source()});
    stage = next;
    show(payload);
    announce('阶段预览已更新');
    await refresh();
  });
}

stages.forEach(button => button.addEventListener('click',()=>preview(button.dataset.stage)));
form.addEventListener('submit', event => {
  event.preventDefault();
  perform(async () => {
    const result = await request('/api/runs',{method:'POST',body:source()});
    stage = 'balance'; show(result,true);
    announce('批次已保存'); await refresh();
  });
});
const filename = document.querySelector('#file-name');
profile.addEventListener('change',()=>{file.value=''; filename.textContent=''; preview(stage);});
file.addEventListener('change',()=>{filename.textContent=file.files[0]?.name || ''; preview(stage);});
document.querySelector('#rows').addEventListener('click',event=>{
  const button = event.target.closest('[data-case]');
  if (button) { selected = button.dataset.case; draw(); document.querySelector(`[data-case="${CSS.escape(selected)}"]`).focus(); }
});
document.querySelector('#history').addEventListener('click',event=>{
  const button = event.target.closest('[data-run]');
  if (button) perform(async()=>{
    const result = await request(`/api/runs/${button.dataset.run}`);
    stage='balance'; show(result,true); announce('已载入历史批次'); await refresh();
  });
});
if (versionSwitch) {
  versionSwitch.addEventListener('click', event => {
    const verifyButton = event.target.closest('[data-verify-original]');
    if (verifyButton && originalBatch) {
      perform(async () => {
        const result = await request(`/api/runs/${originalBatch.id}/verify`);
        const groups = Object.keys(result.visible || {}).length;
        announce(result.verified
          ? `原快照复验一致：${groups} 个业务组，审计可见行未改写`
          : '原快照复验未通过，请检查来源与清单');
      });
      return;
    }
    const button = event.target.closest('[data-version-view]');
    if (!button || !originalBatch) return;
    viewMode = button.dataset.versionView;
    const shown = displayedBatch();
    report = shown.report;
    selected = report.cases?.[0]?.case;
    draw();
    announce(viewMode === 'original' ? '正在回看审计原快照' : '正在查看补录后的当前结果');
  });
}
if (supplementForm) {
  supplementForm.addEventListener('submit', event => {
    event.preventDefault();
    perform(async () => {
      if (!batch?.id) throw new Error('请先执行或选择原批次');
      if (!supplementFile.files.length) throw new Error('请选择补录 CSV');
      const targetId = currentBatch?.id || batch.id;
      let key = supplementKey.value.trim();
      if (!key) {
        key = (globalThis.crypto?.randomUUID?.() || `supplement-${Date.now()}-${Math.random().toString(16).slice(2)}`);
        supplementKey.value = key;
      }
      const data = new FormData();
      data.append('file', supplementFile.files[0]);
      data.append('idempotency_key', key);
      const result = await request(`/api/runs/${targetId}/supplements`, {method:'POST', body:data});
      stage = 'balance';
      show(result, true);
      announce(result.status === 'HELD'
        ? `当前版本已暂缓，原记录：${result.marked_records.join('、') || '待核对'}`
        : '补录当前结果已生成，原快照保持不变');
      await refresh();
    });
  });
}
detail.addEventListener('submit',event=>{
  event.preventDefault();
  perform(async()=>{
    const payload={status:event.submitter.value,revision:batch.revision,note:detail.elements.note.value};
    const result=await request(`/api/runs/${batch.id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    show(result,true); announce(result.status==='APPROVED'?'批次已签发':'批次已暂缓'); await refresh();
  });
});
preview(stage);
