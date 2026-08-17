const STATES = ['validating_credentials','initializing','planning','provisioning','waiting_for_cloud_init','checking_wireguard','verifying_egress','ready'];
const $ = (id) => document.getElementById(id);
let providers = [], locations = [], legacyStates = [], deploymentId = null, operationId = null;
let currentRecord = null, selectedRecoveryId = null, startedAt = null, timer = null;
const api = () => window.pywebview?.api;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function selectedProvider(){ return providers.find((p) => p.id === $('provider').value); }
function selectedLocation(){ return locations.find((l) => l.id === $('location').value); }
function option(el, value, label){ const node=document.createElement('option'); node.value=value; node.textContent=label; el.append(node); }
function setBusy(busy){ $('deploy').disabled=busy || providerBlocked(); $('validate-credentials').disabled=busy; $('cancel').disabled=!busy || !deploymentId; $('provider').disabled=busy; $('country').disabled=busy; $('location').disabled=busy; }
function stateLabel(value){ return String(value).replaceAll('_',' ').replace(/^./,(c)=>c.toUpperCase()); }
function renderSteps(state){ $('steps').replaceChildren(...STATES.map((value)=>{ const li=document.createElement('li'); li.textContent=stateLabel(value); const index=STATES.indexOf(state); li.className=STATES.indexOf(value)<index?'done':value===state?'active':''; return li; })); }
function providerBlocked(){ return legacyStates.some((item)=>item.provider_id===$('provider').value && item.blocking); }
function setState(record){
  if(!record)return;
  currentRecord={...(currentRecord||{}),...record}; deploymentId=currentRecord.id||deploymentId;
  const state=currentRecord.state||'idle'; $('status-text').textContent=stateLabel(state); $('status-dot').className=`status-indicator ${state}`;
  $('ip-address').textContent=currentRecord.public_ip||'—'; $('copy-ip').disabled=!currentRecord.public_ip;
  $('expires').textContent=currentRecord.expires_at?new Date(currentRecord.expires_at).toLocaleString():'—';
  const cloudPossible=Boolean(currentRecord.resources_possible||currentRecord.apply_started_at);
  const terminal=['failed','cancelled','ready'].includes(state);
  $('destroy').disabled=!deploymentId||!cloudPossible||state==='destroyed'||state==='destroying';
  $('remove-local').disabled=!deploymentId||cloudPossible||!terminal;
  renderSteps(state);
}
function resetDeploymentState(){
  deploymentId=null; currentRecord=null; selectedRecoveryId=null;
  $('logs').textContent=''; $('qr-row').classList.add('hidden');
  setState({state:'idle',public_ip:null,expires_at:null,resources_possible:false,apply_started_at:null});
}
function refreshLocations(){ const provider=selectedProvider(); locations=provider?.locations||[]; const countries=[...new Map(locations.map((l)=>[l.country_code,l.country_name])).entries()]; $('country').replaceChildren(); countries.forEach(([id,name])=>option($('country'),id,name)); refreshRegions(); setBusy(false); }
function refreshRegions(){ const country=$('country').value; const values=locations.filter((l)=>l.country_code===country); $('location').replaceChildren(); values.forEach((l)=>option($('location'),l.id,`${l.city} — ${l.region}`)); refreshBadges(); }
function refreshBadges(){ const location=selectedLocation(); if(!location)return; $('server-type').textContent=location.server_type; $('streaming').textContent=location.streaming_status; $('cost').textContent=location.estimated_hourly_cost_usd==null?'Cost unavailable':`~$${location.estimated_hourly_cost_usd.toFixed(3)}/hour`; }
function deploymentOptions(){
  const allowed=$('traffic-mode').value==='ipv4'?['0.0.0.0/0']:$('allowed-ips').value.split(',').map(v=>v.trim()).filter(Boolean);
  const dns=$('dns').value.split(',').map(v=>v.trim()).filter(Boolean); if(!dns.length)throw new Error('Enter at least one DNS server.');
  return {allowed_ips:allowed,enable_ipv6:false,dns_servers:dns,wireguard_port:Number($('port').value),client_mtu:1420,persistent_keepalive:25,ssh_cidr:$('ssh-cidr').value.trim()||null,expiration_minutes:$('expiration').value?Number($('expiration').value):null,automatic_expiration:Boolean($('expiration').value)};
}
async function deploy(){
  if(providerBlocked())throw new Error('This provider has unreconciled legacy Terraform state. Review the recovery warning first.');
  selectedRecoveryId=null;
  setBusy(true); startedAt=Date.now(); startTimer(); const response=await api().start_deploy($('provider').value,$('location').value,deploymentOptions()); operationId=response.operation_id; deploymentId=response.deployment_id;
  while(true){ const status=await api().operation_status(operationId); if(status.deployment){setState(status.deployment); await refreshLogs();} if(status.status==='complete'){ const result=status.result; deploymentId=result.deployment_id; try{setState(await api().get_status(deploymentId));selectedRecoveryId=deploymentId;}catch{deploymentId=null;currentRecord=null;selectedRecoveryId=null;} if(result.status!=='success')throw new Error(result.message); await showConfig(result.config); break;} await sleep(500); }
  setBusy(false);
}
async function refreshLogs(){ if(!deploymentId)return; const lines=await api().get_logs(deploymentId); $('logs').textContent=lines.join('\n'); $('logs').scrollTop=$('logs').scrollHeight; }
async function showConfig(config){ const node=$('qrcode'); $('qr-row').classList.remove('hidden'); node.replaceChildren(); if(window.QRCode){new QRCode(node,{text:config,width:152,height:152,correctLevel:QRCode.CorrectLevel.M});}else{node.textContent='QR library unavailable. Save the configuration instead.';} }
async function destroy(){ if(!deploymentId||!confirm('Destroy this deployment and remove sensitive local recovery artifacts?'))return; setBusy(true); try{const result=await api().destroy(deploymentId,false); if(result.status!=='success')throw new Error(result.message); setState(await api().get_status(deploymentId)); $('qr-row').classList.add('hidden');}catch(e){alert(e.message);} finally{setBusy(false); await loadRecovery();} }
async function removeLocal(){
  if(!deploymentId||selectedRecoveryId!==deploymentId)return;
  try{await api().get_status(deploymentId);}catch{resetDeploymentState();await loadRecovery();return;}
  if(!confirm('Remove this local pre-apply deployment and its generated files?'))return;
  try{await api().remove_local_deployment(deploymentId);resetDeploymentState();await loadRecovery();}catch(e){alert(e.message);await loadRecovery();}
}
async function selectRecovery(item){
  try{
    const fresh=await api().get_status(item.id);
    if(!fresh||fresh.id!==item.id)throw new Error('Recovery deployment no longer exists.');
    selectedRecoveryId=item.id; deploymentId=item.id; currentRecord=null; setState(fresh); await refreshLogs();
  }catch{resetDeploymentState();await loadRecovery();}
}
async function loadRecovery(){
  const items=await api().list_recovery_deployments();
  const refreshedSelection=RecoveryState.selectionAfterRefresh(selectedRecoveryId,items);
  if(selectedRecoveryId&&refreshedSelection===null)resetDeploymentState(); else selectedRecoveryId=refreshedSelection;
  $('recovery').classList.toggle('hidden',!items.length);
  $('recovery-list').replaceChildren(...items.map((item)=>{const button=document.createElement('button');button.className='recovery-item';button.textContent=`${item.provider_id} / ${item.location_id} — ${stateLabel(item.state)}`;button.onclick=()=>selectRecovery(item);return button;}));
}
function staleConfirmationText(item){
  const resources=(item.resource_summary||[]).map((resource)=>{const ids=(resource.identifiers||[]).join(', ')||'no safe identifier recorded';return `- ${resource.address} [${ids}]`;});
  const phrase='I have independently verified that all listed cloud resources are absent.';
  return {phrase,message:[
    'This action does not contact the cloud provider.',
    'You must independently verify that every listed resource no longer exists.',
    '',`Provider: ${item.provider_id}`,`Managed resources: ${item.primary_resources}`,`Terraform lineage: ${item.primary_lineage}`,`Terraform serial: ${item.primary_serial}`,`SHA-256: ${String(item.primary_sha256).slice(0,12)}`,'Resources:',...resources,'','Type this exact sentence to continue:',phrase
  ].join('\n')};
}
async function reconcileStale(item){
  const confirmation=staleConfirmationText(item);
  if(prompt(confirmation.message,'')!==confirmation.phrase)return;
  try{await api().reconcile_stale_legacy_state(item.provider_id,true);await loadLegacyStates();await loadRecovery();}catch(e){alert(e.message);await loadLegacyStates();}
}
async function loadLegacyStates(){
  legacyStates=await api().list_legacy_states(); const visible=legacyStates.filter((item)=>item.classification!=='none');
  $('legacy-recovery').classList.toggle('hidden',!visible.length);
  $('legacy-list').replaceChildren(...visible.map((item)=>{
    const row=document.createElement('div');row.className='recovery-item legacy-item';
    const text=document.createElement('span');const classification=item.classification.replaceAll('-',' ');text.textContent=`${item.provider_id}: ${classification} — ${item.reason}`;row.append(text);
    if(item.reconciliation){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='Reconciliation details';const body=document.createElement('p');body.className='muted-text';body.textContent=`Confirmed ${new Date(item.reconciliation.reconciled_at).toLocaleString()} · ${String(item.reconciliation.fingerprint).slice(0,12)} · ${item.reconciliation.resource_count} resources · ${item.reconciliation.quarantine_path}`;details.append(summary,body);row.append(details);}
    if(item.migration_available){const button=document.createElement('button');button.className='btn';button.textContent='Copy into matched deployment runtime';button.onclick=async()=>{if(confirm('Create a timestamped runtime backup and copy this state into its uniquely matched deployment? The original will remain unchanged.')){try{await api().migrate_legacy_state(item.provider_id);await loadLegacyStates();await loadRecovery();}catch(e){alert(e.message);}}};row.append(button);}
    if(item.stale_reconciliation_available){const button=document.createElement('button');button.className='btn';button.textContent='Mark stale — cloud absence confirmed';button.onclick=()=>reconcileStale(item);row.append(button);}
    return row;
  })); setBusy(false);
}
function startTimer(){ clearInterval(timer); timer=setInterval(()=>{if(startedAt){const seconds=Math.floor((Date.now()-startedAt)/1000);$('elapsed').textContent=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${String(seconds%60).padStart(2,'0')}`;}},1000); }
async function validateCredentials(){const result=await api().validate_credentials($('provider').value);alert(result.message);}
async function initialize(){ if(!api()){setTimeout(initialize,100);return;} providers=await api().list_providers(); $('provider').replaceChildren(); providers.forEach((p)=>option($('provider'),p.id,p.display_name)); refreshLocations(); renderSteps('idle'); await loadLegacyStates(); await loadRecovery(); setInterval(()=>{if(!document.hidden)loadRecovery().catch(()=>{});},5000); }
$('provider').addEventListener('change',()=>{refreshLocations();setBusy(false);}); $('country').addEventListener('change',refreshRegions); $('location').addEventListener('change',refreshBadges); $('traffic-mode').addEventListener('change',()=>{$('allowed-ips').disabled=$('traffic-mode').value!=='custom';if($('traffic-mode').value==='ipv4')$('allowed-ips').value='0.0.0.0/0';});
$('deploy').addEventListener('click',()=>deploy().catch((e)=>{alert(e.message);setBusy(false);})); $('validate-credentials').addEventListener('click',()=>validateCredentials().catch((e)=>alert(e.message))); $('destroy').addEventListener('click',destroy); $('remove-local').addEventListener('click',removeLocal); $('cancel').addEventListener('click',async()=>{if(deploymentId)await api().cancel(deploymentId);});
$('copy-ip').addEventListener('click',()=>navigator.clipboard.writeText($('ip-address').textContent)); $('save-config').addEventListener('click',async()=>{const destination=prompt('Save to an absolute path (for example C:\\Users\\you\\WireGuard\\vpn.conf):');if(destination)await api().save_client_config(deploymentId,destination);});
window.addEventListener('beforeunload',(event)=>{if(currentRecord?.resources_possible&&currentRecord.state!=='destroyed'){event.preventDefault();event.returnValue='Active cloud resources may still exist.';}}); document.addEventListener('DOMContentLoaded',initialize);
