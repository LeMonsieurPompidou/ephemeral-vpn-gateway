const STATES = ['validating_credentials','initializing','planning','provisioning','waiting_for_cloud_init','checking_wireguard','verifying_egress','ready'];
const $ = (id) => document.getElementById(id);
let providers = [], locations = [], legacyStates = [], deploymentId = null, operationId = null;
let activeDeploymentId = null, currentRecord = null, selectedRecoveryId = null, timer = null;
let syncInFlight = false, uiBusy = false, configExportProposal = null;
let clientMetadata = [], selectedClientId = null, recoveryItems = [];
const api = () => window.pywebview?.api;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function selectedProvider(){ return providers.find((p) => p.id === $('provider').value); }
function selectedLocation(){ return locations.find((l) => l.id === $('location').value); }
function option(el, value, label){ const node=document.createElement('option'); node.value=value; node.textContent=label; el.append(node); }
function updateActionButtons(){const state=currentRecord?.state||'idle';const cloudPossible=Boolean(currentRecord?.resources_possible||currentRecord?.apply_started_at);const terminal=['failed','cancelled','ready'].includes(state);$('destroy').disabled=uiBusy||!deploymentId||!cloudPossible||state==='destroyed'||state==='destroying';$('remove-local').disabled=uiBusy||!deploymentId||cloudPossible||!terminal;}
function setBusy(busy){ uiBusy=busy; $('deploy').disabled=busy || providerBlocked(); $('validate-credentials').disabled=busy; $('cancel').disabled=!busy || !deploymentId; $('provider').disabled=busy; $('country').disabled=busy; $('location').disabled=busy; $('expiration').disabled=busy; $('client-count').disabled=busy; updateActionButtons(); }
function stateLabel(value){ return String(value).replaceAll('_',' ').replace(/^./,(c)=>c.toUpperCase()); }
function renderSteps(state){ $('steps').replaceChildren(...STATES.map((value)=>{ const li=document.createElement('li'); li.textContent=stateLabel(value); const index=STATES.indexOf(state); li.className=STATES.indexOf(value)<index?'done':value===state?'active':''; return li; })); }
function selectedLegacyBlockers(){return RecoveryState.blockingLegacyStatesForProvider(legacyStates,$('provider').value);}
function providerBlocked(){ return selectedLegacyBlockers().length>0; }
function setState(record, expectedId=deploymentId){
  if(!RecoveryState.acceptsBackendRecord(currentRecord,record,expectedId))return false;
  currentRecord=currentRecord?.id===record.id?{...currentRecord,...record}:{...record}; deploymentId=expectedId;
  const state=currentRecord.state||'idle'; $('status-text').textContent=RecoveryState.cloudInitStatus(currentRecord)||stateLabel(state); $('status-dot').className=`status-indicator ${state}`;
  $('ip-address').textContent=currentRecord.public_ip||'—'; $('copy-ip').disabled=!currentRecord.public_ip;
  $('expires').textContent=currentRecord.expires_at?new Date(currentRecord.expires_at).toLocaleString():'—';
  const cleanupWarnings=Array.isArray(currentRecord.local_cleanup_warnings)?currentRecord.local_cleanup_warnings:[];
  $('local-cleanup-warning').textContent=cleanupWarnings.join('\n');$('local-cleanup-warning').classList.toggle('hidden',!cleanupWarnings.length);
  updateSessionEstimate();
  updateActionButtons();
  renderSteps(state);
  return true;
}
function resetDeploymentState(){
  deploymentId=null; activeDeploymentId=null; currentRecord=null; selectedRecoveryId=null;
  $('logs').textContent=''; $('qr-row').classList.add('hidden'); resetClientConfigs();
  $('status-text').textContent='Idle'; $('status-dot').className='status-indicator idle';
  $('ip-address').textContent='—'; $('copy-ip').disabled=true; $('expires').textContent='—';
  $('running-label').textContent='Running'; $('running-time').textContent='—'; $('estimated-cost').textContent='Unavailable';
  $('local-cleanup-warning').textContent='';$('local-cleanup-warning').classList.add('hidden');
  $('destroy').disabled=true; $('remove-local').disabled=true; renderSteps('idle');
}
function refreshLocations(){ const provider=selectedProvider(); locations=provider?.locations||[]; const countries=[...new Map(locations.map((l)=>[l.country_code,l.country_name])).entries()]; $('country').replaceChildren(); countries.forEach(([id,name])=>option($('country'),id,name)); refreshRegions(); setBusy(false); }
function refreshRegions(){ const country=$('country').value; const values=locations.filter((l)=>l.country_code===country); $('location').replaceChildren(); values.forEach((l)=>option($('location'),l.id,`${l.city} — ${l.region}`)); refreshBadges(); }
function refreshBadges(){ const location=selectedLocation(); if(!location)return; $('server-type').textContent=location.server_type; $('streaming').textContent=location.streaming_status; $('cost').textContent=location.estimated_hourly_cost_usd==null?'Estimated cost unavailable':`~$${location.estimated_hourly_cost_usd.toFixed(3)}/hour`; }
function deploymentOptions(){
  const clientCount=Number($('client-count').value);
  if(!Number.isInteger(clientCount)||clientCount<1||clientCount>10)throw new Error('VPN clients must be between 1 and 10.');
  return {client_count:clientCount,expiration_minutes:$('expiration').value?Number($('expiration').value):null,automatic_expiration:Boolean($('expiration').value)};
}
async function deploy(){
  if(providerBlocked())throw new Error('Deployment recovery is required before creating a new gateway. Open Recovery for the available actions.');
  if(operationId||activeDeploymentId)throw new Error('Another deployment operation is already active.');
  selectedRecoveryId=null;
  setBusy(true);
  const response=await api().start_deploy($('provider').value,$('location').value,deploymentOptions());
  if(response.status!=='started')throw new Error(response.message||'Deployment could not be started.');
  const startedOperationId=response.operation_id; operationId=startedOperationId;
  activeDeploymentId=response.deployment_id; deploymentId=activeDeploymentId; currentRecord=null;
  try{
    while(operationId===startedOperationId){
      const status=await api().operation_status(startedOperationId);
      if(status.status==='error')throw new Error(status.message||'Deployment status is unavailable.');
      if(status.deployment?.id===activeDeploymentId)setState(status.deployment,activeDeploymentId);
      await refreshLogs(activeDeploymentId);
      if(status.status==='complete'){
        const result=status.result;
        if(result.deployment_id!==activeDeploymentId)throw new Error('Backend returned a mismatched deployment operation.');
        try{setState(await api().get_status(activeDeploymentId),activeDeploymentId);selectedRecoveryId=activeDeploymentId;}catch{selectedRecoveryId=null;}
        if(result.status!=='success')throw new Error(result.message);
        await showClients(activeDeploymentId); break;
      }
      await sleep(500);
    }
  }finally{
    if(operationId===startedOperationId)operationId=null;
    activeDeploymentId=null; setBusy(false); await loadRecovery();
  }
}
async function refreshLogs(targetId=deploymentId){
  if(!targetId)return; let lines;
  try{lines=await api().get_logs(targetId);}catch{return;}
  if(!RecoveryState.canRenderLogs(targetId,deploymentId,activeDeploymentId))return;
  $('logs').textContent=lines.join('\n'); $('logs').scrollTop=$('logs').scrollHeight;
}
function resetConfigExport(){configExportProposal=null;$('config-save-path').textContent='Preparing Desktop location…';$('config-save-status').textContent='';$('save-config').disabled=true;}
function resetClientConfigs(){clientMetadata=[];selectedClientId=null;$('client-list').replaceChildren();$('qrcode').replaceChildren();resetConfigExport();}
function renderClientTabs(targetId){
  $('client-list').replaceChildren(...clientMetadata.map((client)=>{const button=document.createElement('button');button.className=`client-tab${client.id===selectedClientId?' active':''}`;button.textContent=client.display_name;button.onclick=()=>selectClient(targetId,client.id).catch(()=>{});return button;}));
}
async function loadConfigExport(targetId,clientId){
  resetConfigExport();
  const proposal=await api().get_client_config_export(targetId,clientId);
  if(targetId!==deploymentId||clientId!==selectedClientId||proposal.deployment_id!==targetId||proposal.client_id!==clientId)return;
  configExportProposal=proposal;$('config-save-path').textContent=proposal.path;$('save-config').disabled=false;
}
async function selectClient(targetId,clientId){
  if(targetId!==deploymentId||!clientMetadata.some((client)=>client.id===clientId))return;
  selectedClientId=clientId;renderClientTabs(targetId);resetConfigExport();
  const selected=clientMetadata.find((client)=>client.id===clientId);$('selected-client-name').textContent=selected?.display_name||'Client';
  const node=$('qrcode');node.replaceChildren();node.textContent='Loading client configuration…';
  let config;
  try{config=await api().get_client_config(targetId,clientId);}catch{if(targetId===deploymentId&&clientId===selectedClientId)node.textContent='Client configuration unavailable.';return;}
  if(targetId!==deploymentId||clientId!==selectedClientId)return;
  node.replaceChildren();
  if(window.QRCode){new QRCode(node,{text:config,width:152,height:152,correctLevel:QRCode.CorrectLevel.M});}else{node.textContent='QR library unavailable. Save the configuration instead.';}
  try{await loadConfigExport(targetId,clientId);}catch{if(targetId===deploymentId&&clientId===selectedClientId){$('config-save-path').textContent='Desktop save location unavailable';$('save-config').disabled=true;}}
}
async function showClients(targetId=deploymentId){
  resetClientConfigs();if(!targetId)return;
  const clients=await api().get_client_configs(targetId);
  if(targetId!==deploymentId||!Array.isArray(clients)||!clients.length)return;
  clientMetadata=clients;$('qr-row').classList.remove('hidden');await selectClient(targetId,clients[0].id);
}
async function saveConfig(){
  const targetId=deploymentId;const clientId=selectedClientId;if(!targetId||!clientId||!configExportProposal||configExportProposal.deployment_id!==targetId||configExportProposal.client_id!==clientId)return;
  $('save-config').disabled=true;$('config-save-status').textContent='';
  try{
    const result=await api().save_client_config(targetId,clientId);
    if(targetId!==deploymentId||clientId!==selectedClientId)return;
    if(result.status==='cancelled'){$('config-save-status').textContent='Save cancelled.';return;}
    if(result.status!=='success')throw new Error(result.message||'Configuration could not be saved.');
    $('config-save-path').textContent=result.path;$('config-save-status').textContent=`Configuration saved: ${result.path}`;
  }catch(e){if(targetId===deploymentId&&clientId===selectedClientId){$('config-save-status').textContent='Configuration was not saved.';alert(e.message);}}
  finally{if(targetId===deploymentId&&clientId===selectedClientId)$('save-config').disabled=false;}
}
async function destroy(){ if(!deploymentId||!confirm('Destroy this deployment and remove sensitive local recovery artifacts?'))return; setBusy(true); try{const result=await api().destroy(deploymentId,false); if(result.status!=='success')throw new Error(result.message); setState(await api().get_status(deploymentId));selectedRecoveryId=null;$('qr-row').classList.add('hidden');resetClientConfigs();}catch(e){alert(e.message);} finally{setBusy(false); await loadRecovery();} }
async function removeLocal(){
  if(!deploymentId||selectedRecoveryId!==deploymentId)return;
  try{await api().get_status(deploymentId);}catch{resetDeploymentState();await loadRecovery();return;}
  if(!confirm('Remove this local pre-apply deployment and its generated files?'))return;
  try{await api().remove_local_deployment(deploymentId);resetDeploymentState();await loadRecovery();}catch(e){alert(e.message);await loadRecovery();}
}
async function selectRecovery(item){
  if(activeDeploymentId)return;
  try{
    const fresh=await api().get_status(item.id);
    if(!fresh||fresh.id!==item.id)throw new Error('Recovery deployment no longer exists.');
    selectedRecoveryId=item.id; deploymentId=item.id; currentRecord=null; setState(fresh,item.id); await refreshLogs(item.id);
    if(fresh.state==='ready')await showClients(item.id);else{$('qr-row').classList.add('hidden');resetClientConfigs();}
  }catch{resetDeploymentState();await loadRecovery();}
}
async function loadRecovery(){
  const items=await api().list_recovery_deployments();
  recoveryItems=items;
  const refreshedSelection=RecoveryState.selectionAfterRefresh(selectedRecoveryId,items);
  if(!activeDeploymentId&&selectedRecoveryId&&refreshedSelection===null)resetDeploymentState();
  else if(!activeDeploymentId)selectedRecoveryId=refreshedSelection;
  updateRecoveryVisibility();
  $('recovery-list').replaceChildren(...items.map((item)=>{const button=document.createElement('button');button.className='recovery-item';button.disabled=Boolean(activeDeploymentId);button.textContent=`${item.provider_id} / ${item.location_id} — ${stateLabel(item.state)}`;button.onclick=()=>selectRecovery(item);return button;}));
}
async function reconcileCurrentDeployment(){
  if(syncInFlight)return;
  const targetId=activeDeploymentId||(selectedRecoveryId===deploymentId?deploymentId:null);
  if(!targetId)return;
  syncInFlight=true;
  try{
    const fresh=await api().get_status(targetId);
    const stillCurrent=targetId===(activeDeploymentId||(selectedRecoveryId===deploymentId?deploymentId:null));
    if(stillCurrent&&fresh?.id===targetId){setState(fresh,targetId);await refreshLogs(targetId);}
  }catch{
    if(!activeDeploymentId&&selectedRecoveryId===targetId)resetDeploymentState();
  }finally{syncInFlight=false;}
}
  function staleConfirmationText(item){
  const resources=(item.resource_summary||[]).map((resource)=>{const ids=(resource.identifiers||[]).join(', ')||'no safe identifier recorded';return `- ${resource.address} [${ids}]`;});
  const phrase='I have independently verified that all listed cloud resources are absent.';
  return {phrase,message:[
    'This action does not contact the cloud provider.',
    'You must independently verify that every listed resource no longer exists.',
      '',`Provider: ${item.provider_id}`,`Managed resources: ${item.source_resources}`,`Terraform lineage: ${item.source_lineage}`,`Terraform serial: ${item.source_serial}`,`SHA-256: ${String(item.source_sha256).slice(0,12)}`,'Resources:',...resources,'','Type this exact sentence to continue:',phrase
    ].join('\n')};
  }
  async function reconcileStale(item){
  const confirmation=staleConfirmationText(item);
  if(prompt(confirmation.message,'')!==confirmation.phrase)return;
  try{await api().reconcile_stale_legacy_state(item.provider_id,true);await loadLegacyStates();await loadRecovery();}catch(e){alert(e.message);await loadLegacyStates();}
  }
  async function verifyLegacyCloud(item){
    const buttonText='Verify cloud state';
    if(!confirm(`Run read-only ${item.provider_id} queries for the exact resource IDs in this legacy state? No cloud resources or Terraform state will be modified.`))return;
    try{
      const result=await api().verify_legacy_cloud_state(item.provider_id);
      await loadLegacyStates();
      if(result.status==='all_absent')alert('No legacy cloud resources were found. Review and explicitly reconcile the preserved state.');
      else alert(result.message||'Legacy cloud verification was inconclusive.');
    }catch(e){alert(e.message||buttonText);await loadLegacyStates();}
  }
  function renderLegacyStates(){
    const visible=selectedLegacyBlockers();
    $('legacy-warning').classList.toggle('hidden',!visible.length);
    $('legacy-tools').classList.toggle('hidden',!visible.length);
    $('legacy-list').replaceChildren(...visible.map((item)=>{
      const row=document.createElement('div');row.className='recovery-item legacy-item';
      const text=document.createElement('span');const classification=item.classification.replaceAll('-',' ');text.textContent=`${item.provider_id}: ${classification} — ${item.reason}`;row.append(text);
      const verification=item.cloud_verification;
      if(verification){const status=document.createElement('span');status.className='muted-text';status.textContent=verification.message||`Cloud verification: ${verification.status}`;row.append(status);}
      if(item.reconciliation){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Reconciliation details';const body=document.createElement('p');body.className='muted-text';body.textContent=`Confirmed ${new Date(item.reconciliation.reconciled_at).toLocaleString()} · ${String(item.reconciliation.fingerprint).slice(0,12)} · ${item.reconciliation.resource_count} resources · ${item.reconciliation.quarantine_path}`;details.append(summary,body);row.append(details);}
      if(item.migration_available){const button=document.createElement('button');button.className='btn';button.textContent='Copy into matched deployment runtime';button.onclick=async()=>{if(confirm('Create a timestamped runtime backup and copy this state into its uniquely matched deployment? The original will remain unchanged.')){try{await api().migrate_legacy_state(item.provider_id);await loadLegacyStates();await loadRecovery();}catch(e){alert(e.message);}}};row.append(button);}
      if(item.cloud_verification_available&&!item.stale_reconciliation_available){const button=document.createElement('button');button.className='btn';button.textContent='Verify cloud state';button.onclick=()=>verifyLegacyCloud(item);row.append(button);}
      if(item.stale_reconciliation_available){const button=document.createElement('button');button.className='btn';button.textContent='Mark stale and reconcile';button.onclick=()=>reconcileStale(item);row.append(button);}
      return row;
    }));
    updateRecoveryVisibility();setBusy(false);
  }
  async function loadLegacyStates(){
    legacyStates=await api().list_legacy_states();renderLegacyStates();
  }
  function updateRecoveryVisibility(){
    const hasLegacy=selectedLegacyBlockers().length>0;
  $('recovery').classList.toggle('hidden',!recoveryItems.length&&!hasLegacy);
  $('recovery-summary').textContent=recoveryItems.length?'These deployments may still own billable resources.':'Resolve the blocking infrastructure state before creating a gateway.';
}
function updateSessionEstimate(){
  const estimate=SessionCost.estimate(currentRecord,Date.now());
  $('running-label').textContent=estimate.finalized?'Session duration':'Running';
  $('running-time').textContent=SessionCost.formatDuration(estimate.elapsedSeconds);
  $('estimated-cost').textContent=SessionCost.formatCost(estimate.costUsd);
}
function startTimer(){clearInterval(timer);timer=setInterval(updateSessionEstimate,1000);updateSessionEstimate();}
function clearCredentialFeedback(){
  $('credential-feedback').className='credential-feedback hidden';$('credential-message').textContent='';$('credential-help').classList.add('hidden');$('credential-help').open=false;$('credential-help-content').replaceChildren();
}
function renderCredentialFeedback(result,expectedProvider){
  if(result.provider_id!==expectedProvider||$('provider').value!==expectedProvider)return;
  const container=$('credential-feedback');container.className=`credential-feedback ${result.valid?'valid':'invalid'}`;$('credential-message').textContent=result.message;
  const help=$('credential-help'),content=$('credential-help-content');content.replaceChildren();
  if(result.reason==='missing'){
    const missing=Array.isArray(result.missing_variables)?result.missing_variables:[];
    if(missing.length){const label=document.createElement('span');label.textContent=`Missing: ${missing.join(', ')}`;content.append(label);}
    for(const command of (result.setup_commands||[])){const code=document.createElement('code');code.className='credential-command';code.textContent=command;content.append(code);}
    for(const note of (result.notes||[])){const text=document.createElement('span');text.className='muted-text';text.textContent=note;content.append(text);}
    help.classList.remove('hidden');
  }else help.classList.add('hidden');
  container.classList.remove('hidden');
}
async function validateCredentials(){
  const providerId=$('provider').value;clearCredentialFeedback();$('validate-credentials').disabled=true;
  try{renderCredentialFeedback(await api().validate_credentials(providerId),providerId);}finally{$('validate-credentials').disabled=uiBusy;}
}
async function initialize(){ if(!api()){setTimeout(initialize,100);return;} providers=await api().list_providers(); $('provider').replaceChildren(); providers.forEach((p)=>option($('provider'),p.id,p.display_name)); refreshLocations(); renderSteps('idle'); startTimer(); await loadLegacyStates(); await loadRecovery(); setInterval(()=>{if(!document.hidden)reconcileCurrentDeployment().catch(()=>{});},1000); setInterval(()=>{if(!document.hidden)loadRecovery().catch(()=>{});},5000); }
  $('provider').addEventListener('change',()=>{clearCredentialFeedback();refreshLocations();renderLegacyStates();setBusy(false);}); $('country').addEventListener('change',refreshRegions); $('location').addEventListener('change',refreshBadges);
$('deploy').addEventListener('click',()=>deploy().catch((e)=>{alert(e.message);setBusy(Boolean(operationId||activeDeploymentId));})); $('validate-credentials').addEventListener('click',()=>validateCredentials().catch((e)=>alert(e.message))); $('destroy').addEventListener('click',destroy); $('remove-local').addEventListener('click',removeLocal); $('cancel').addEventListener('click',async()=>{if(deploymentId)await api().cancel(deploymentId);});
$('copy-ip').addEventListener('click',()=>navigator.clipboard.writeText($('ip-address').textContent)); $('save-config').addEventListener('click',()=>saveConfig());
$('open-recovery').addEventListener('click',()=>{$('legacy-tools').open=true;$('recovery').scrollIntoView({behavior:'smooth',block:'start'});});
window.addEventListener('beforeunload',(event)=>{if(currentRecord?.resources_possible&&currentRecord.state!=='destroyed'){event.preventDefault();event.returnValue='Active cloud resources may still exist.';}}); document.addEventListener('DOMContentLoaded',initialize);
