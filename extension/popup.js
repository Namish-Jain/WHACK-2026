import {normalizeProfile} from './logic.mjs';

const API = 'http://127.0.0.1:8765';
const RISK_LABELS = {low:'Low bot risk', moderate:'Moderate bot risk', high:'High bot risk', very_high:'Very high bot risk', insufficient_data:'Not enough data'};
const $ = id => document.getElementById(id);
const local = chrome.storage.local;
let token = '', pollTimer, busy = false, ready = false, retryId = null, currentStep = 0;
const STEP_LABELS = ['Reading followers', 'Analysing patterns', 'Writing explanation'];

async function request(path, body) {
  let response;
  try {
    response = await fetch(API + path, {method:body ? 'POST' : 'GET',
      headers:{'X-Audience-Token':token, ...(body ? {'Content-Type':'application/json'} : {})},
      ...(body ? {body:JSON.stringify(body)} : {}), signal:AbortSignal.timeout(path === '/settings' ? 20000 : 10000)});
  } catch { throw new Error('DeTect is offline. Start the local service, then reconnect.'); }
  const value = await response.json();
  if (!response.ok) {
    if (response.status === 401 || response.status === 428) $('settings').open = true;
    throw new Error(value.error || 'The request could not finish. Please try again.');
  }
  return value;
}

function setBusy(value) {
  busy = value;
  ['analyze','current-tab','profile','retry'].forEach(id => $(id).disabled = value);
  $('analyze').textContent = value ? 'Analysing…' : 'Analyse';
}
function showProgress(step, state = 'running', skipped = false) {
  currentStep = Math.max(0, Math.min(3, step));
  $('progress').hidden = false;
  for (let i = 0; i < 3; i++) {
    let status = i < currentStep ? 'completed' : 'pending';
    if (i === currentStep) status = state === 'running' ? 'active' : state;
    if (skipped && i === 2) status = 'skipped';
    $('step-' + i).className = 'step ' + status;
    $('step-' + i).setAttribute('aria-label', STEP_LABELS[i] + ': ' + status);
    $('step-' + i).setAttribute('aria-current', status === 'active' ? 'step' : 'false');
    $('node-' + i).textContent = status === 'completed' ? '✓' : status === 'failed' ? '!' : status === 'skipped' ? '–' : String(i + 1);
  }
  $('stage').textContent = currentStep === 3 ? (skipped ? 'Analysis complete. Explanation not needed.' : 'Analysis complete.')
    : STEP_LABELS[currentStep] + (state === 'running' ? '' : ': ' + state);
}
function jobStep(job) {
  if (Number.isInteger(job.step)) return job.step;
  // Accept stages from a companion service that has not yet been restarted.
  if (/explanation/i.test(job.stage)) return 2;
  if (/patterns/i.test(job.stage)) return 1;
  return 0;
}
function pauseProgress() { if (!$('progress').hidden && currentStep < 3) showProgress(currentStep, 'paused'); }
function showError(message) { $('error').textContent = message; $('error').hidden = false; }
function hideResult() { $('progress').hidden = true; $('result').hidden = true; $('retry').hidden = true; retryId = null; }
function render(result) {
  // Old fallback reports must never be presented as a new Gemini explanation.
  if (result.presentation_version !== 2 || (result.score !== null && !result.gemini_reason)) return;
  const score = result.score;
  const scored = Number.isFinite(score) && score >= 0 && score <= 100;
  $('score-panel').hidden = !scored;
  if (scored) {
    $('score').textContent = score.toFixed(1);
    $('risk-marker').style.left = score + '%';
    $('risk-scale').setAttribute('aria-valuenow', String(score));
    $('risk-scale').setAttribute('aria-valuetext', score.toFixed(1) + ' out of 100, ' + (RISK_LABELS[result.severity] || ''));
  }
  const sampled = Number.isInteger(result.sample_size) && result.sample_size >= 0;
  $('sample-count').hidden = !sampled;
  $('sample-count').textContent = sampled ? result.sample_size + ' followers analysed' : '';
  $('rating').textContent = RISK_LABELS[result.severity] || 'Not enough data';
  $('summary').textContent = result.score === null
    ? 'There is not enough public follower information to assess this profile.' : result.gemini_reason;
  const stats = result.stats_analysis;
  $('stats-analysis').hidden = !stats;
  if (stats) {
    $('stat-fit').textContent = Number.isFinite(stats.goodness_of_fit) ? stats.goodness_of_fit.toFixed(2) : 'Not available';
    const p = stats.adjusted_p_value;
    $('stat-p').textContent = !Number.isFinite(p) ? 'Not available' : stats.p_value_at_floor && p <= 1e-300
      ? '≤ 1e-300' : p > 0 && p < .001 ? p.toExponential(2) : p.toFixed(3);
    for (const [id, value] of [['stat-ratio', stats.following_imbalance], ['stat-activity', stats.low_activity]]) {
      $(id).textContent = Number.isFinite(value) ? (100 * value).toFixed(1) + '%' : 'Not available';
    }
  }
  $('result').hidden = false;
}
async function checkConnection() {
  const health = await request('/health');
  if (health.service !== 'detest' || health.api_version !== 2)
    throw new Error('Restart the local service to finish updating DeTect.');
  $('connection').textContent = 'Connected'; $('connection').className = 'connection online';
  ready = health.gemini_available;
  $('model').value = health.model || '';
  $('api-key').placeholder = ready ? 'Key saved. Enter a new key to replace it.' : 'Enter API key';
  if (!ready) $('settings').open = true;
}
async function poll(id) {
  clearTimeout(pollTimer);
  try {
    const job = await request('/jobs/' + id);
    if (job.state === 'running') {
      setBusy(true); showProgress(jobStep(job));
      pollTimer = setTimeout(() => poll(id), 1200);
    } else {
      setBusy(false); await local.remove('activeJob');
      if (job.state === 'complete') {
        showProgress(3, 'completed', job.result.score === null);
        render(job.result); await local.set({lastResult:job.result}); await local.remove('retryJob');
      } else if (job.state === 'explanation_failed') {
        hideResult(); showProgress(2, 'failed'); retryId = id; $('retry').hidden = false;
        await local.set({retryJob:id}); showError(job.error);
      } else { await local.remove('retryJob'); hideResult(); showProgress(jobStep(job), 'failed'); showError(job.error || 'Unable to assess this profile.'); }
    }
  } catch (error) { setBusy(false); pauseProgress(); showError(error.message); }
}
$('analyze-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  $('error').hidden = true;
  try {
    const account = normalizeProfile($('profile').value);
    if (!token) { $('settings').open = true; throw new Error('Enter your connection code in Connection.'); }
    if (!ready) { $('settings').open = true; $('api-key').focus(); throw new Error('Add your Gemini key in Connection to continue.'); }
    hideResult(); setBusy(true); showProgress(0);
    await local.set({profile:account}); await local.remove('lastResult'); await local.remove('retryJob');
    const job = await request('/jobs', {account, mode:'fresh', use_gemini:true});
    await local.set({activeJob:job.id}); await poll(job.id);
  } catch (error) { setBusy(false); pauseProgress(); showError(error.message); }
});
$('current-tab').addEventListener('click', async () => {
  try {
    const [tab] = await chrome.tabs.query({active:true, currentWindow:true});
    $('profile').value = normalizeProfile(tab?.url || '');
    $('error').hidden = true; hideResult();
  } catch (error) { showError(error.message); }
});
$('connect').addEventListener('click', async () => {
  token = $('token').value.trim(); await local.set({token}); $('error').hidden = true;
  try {
    await checkConnection(); $('settings').open = !ready;
    const saved = await local.get(['activeJob','retryJob']);
    if (saved.activeJob || saved.retryJob) await poll(saved.activeJob || saved.retryJob);
  } catch (error) { ready = false; $('connection').textContent = 'Offline'; $('connection').className = 'connection'; showError(error.message); }
});
$('key-form').addEventListener('submit', async event => {
  event.preventDefault(); if ($('save-key').disabled) return;
  $('save-key').disabled = true; $('api-key').disabled = true; $('model').disabled = true; $('error').hidden = true;
  $('settings-message').textContent = 'Checking key';
  const apiKey = $('api-key').value;
  try {
    if (!token) throw new Error('Connect to DeTect before saving your key.');
    const settings = await request('/settings', {api_key:apiKey, model:$('model').value.trim() || null});
    ready = settings.gemini_available; $('model').value = settings.model; $('api-key').value = '';
    $('api-key').placeholder = 'Key saved. Enter a new key to replace it.';
    $('settings-message').textContent = 'Saved'; $('settings').open = false;
  } catch (error) { $('settings-message').textContent = ''; showError(error.message); }
  finally { $('save-key').disabled = false; $('api-key').disabled = false; $('model').disabled = false; }
});
$('retry').addEventListener('click', async () => {
  if (!retryId || busy) return;
  $('error').hidden = true; setBusy(true); showProgress(2);
  try {
    const job = await request('/jobs/' + retryId + '/retry', {});
    await local.set({activeJob:job.id}); await poll(job.id);
  } catch (error) { setBusy(false); pauseProgress(); showError(error.message); }
});
$('profile').addEventListener('input', () => { hideResult(); $('error').hidden = true; });
async function init() {
  if (local.setAccessLevel) await local.setAccessLevel({accessLevel:'TRUSTED_CONTEXTS'});
  const saved = await local.get(['token','profile','activeJob','retryJob','lastResult']);
  token = saved.token || ''; $('token').value = token; $('profile').value = saved.profile || '';
  if (saved.lastResult && !saved.activeJob && !saved.retryJob) render(saved.lastResult);
  if (!token) { $('settings').open = true; return; }
  try { await checkConnection(); if (saved.activeJob || saved.retryJob) await poll(saved.activeJob || saved.retryJob); }
  catch (error) { showError(error.message); }
}
init().catch(error => showError(error.message));
