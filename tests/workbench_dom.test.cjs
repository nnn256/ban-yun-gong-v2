const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
function element() {
  return {value:'',textContent:'',hidden:false,children:[],className:'',href:'',title:'',disabled:false,attributes:{},listeners:{},
    classList:{add(){},remove(){},toggle(){}},get options(){return this.children},
    setAttribute(name,value){this.attributes[name]=value},replaceChildren(...children){this.children=children;this.value=children[0]?.value||''},append(...children){this.children.push(...children)},
    addEventListener(type,handler){this.listeners[type]=handler},focus(){},showModal(){this.open=true},close(){this.open=false},reset(){}}
}
const nodes={toast:element()}
const suffixes=['Platform','Instance','InstanceOptions','Account','AccountLabel','AccountHelp','MessageType','Session','SessionOptions','SessionLabel','SessionHelp','SessionSearch','SessionPicker']
for(const side of ['source','target']) for(const suffix of suffixes) nodes[side+suffix]=element()
for(const id of ['qqAccountSummary','qqIdentityState','qqAccountsList','qqForwardActivity','qqState','qqDot','openSnowLumaLocal','qqOneBotAddress','launchLocalQq','bridgeToggle']) nodes[id]=element()
for(const id of ['mappingIncludeSelf','mappingDirection','mappingDialogTitle','submitMapping']) nodes[id]=element()
const contentTypeValues=['text','emoji','image','video','audio','file']
const contentTypeInputs=contentTypeValues.map(value=>({...element(),name:'contentType',value,checked:true}))
const context=vm.createContext({URL,URLSearchParams,setTimeout(){},clearTimeout(){},location:{href:'http://127.0.0.1:18766/',search:'',replace(){}},localStorage:{getItem(){return null}},document:{documentElement:{dataset:{}},querySelector(selector){return nodes[selector.slice(1)]||null},querySelectorAll(selector){return selector==='#contentTypeOptions input[name="contentType"]'?contentTypeInputs:selector.endsWith(':checked')?contentTypeInputs.filter(input=>input.checked):[]},createElement:element}})
vm.runInContext(fs.readFileSync(path.join(__dirname,'../workbench/app.js'),'utf8'),context)
vm.runInContext(`state.mappingDiscovery={qq_instances:[{id:'qq_bridge',accounts:[
 {user_id:'111',nickname:'First',connected:true,identity_verified:true,groups:[{group_id:'100',group_name:'Alpha Team'},{group_id:'300',group_name:'Shared Team'}]},
 {user_id:'222',nickname:'Second',connected:true,identity_verified:true,groups:[{group_id:'200',group_name:'Beta Team'},{group_id:'300',group_name:'Shared Team'}]}
]}],feishu_accounts:[{id:'default'}],feishu_chats:[{chat_id:'123456789',name:'Design Team',message_type:'GroupMessage'},{chat_id:'901',name:'Project Two',message_type:'GroupMessage'},{chat_id:'987654321',name:'Design Team',message_type:'GroupMessage'}]}`,context)
for(const side of ['source','target']) {nodes[side+'Platform'].value='aiocqhttp';nodes[side+'Instance'].value='qq_bridge';nodes[side+'MessageType'].value='GroupMessage'}
nodes.sourceSession.value='100'
vm.runInContext("renderEndpoint('source')",context)
assert.equal(nodes.sourceAccount.value,'111')
assert.match(nodes.sourceSessionPicker.children[1].textContent,/^Alpha Team · QQ群$/)
nodes.sourceAccount.value='';nodes.sourceSession.value='300'
vm.runInContext("renderEndpoint('source')",context)
assert.equal(nodes.sourceAccount.value,'','Shared group cannot pick an arbitrary QQ')
nodes.sourceAccount.value='222'
vm.runInContext("renderEndpoint('source')",context)
assert.deepEqual(nodes.sourceSessionPicker.children.filter(x=>x.value).map(x=>x.value),['200','300'])
nodes.targetAccount.value='111'
vm.runInContext("renderEndpoint('target')",context)
assert.equal(nodes.targetAccount.value,'111','Target QQ account is independent of source')
nodes.sourceSessionSearch.value='Beta'
vm.runInContext("renderEndpoint('source')",context)
assert.deepEqual(nodes.sourceSessionPicker.children.filter(x=>x.value).map(x=>x.value),['300','200'],'The selected QQ group stays visible alongside search results')
assert.equal(nodes.sourceSessionPicker.value,'300','Searching does not clear the selected QQ group')
nodes.targetPlatform.value='larkx';nodes.targetInstance.value='';nodes.targetSession.value='123456789'
vm.runInContext("renderEndpoint('target')",context)
assert.equal(nodes.targetAccount.hidden,true)
assert.equal(nodes.targetInstance.value,'default')
assert.match(nodes.targetSessionPicker.children[1].textContent,/^Design Team · 飞书群 · …456789$/)
assert.match(nodes.targetSessionHelp.textContent,/已选择飞书会话“Design Team”/)
assert.doesNotMatch(nodes.targetSessionPicker.children[1].textContent,/123456789/,'The picker keeps the full ID in the separate editable field')
assert.equal(nodes.targetSessionPicker.children[3].textContent,'Design Team · 飞书群 · …654321','Duplicate names receive a six-character ID suffix')
nodes.targetSessionSearch.value='987654321'
vm.runInContext("renderEndpoint('target')",context)
assert.deepEqual(nodes.targetSessionPicker.children.filter(x=>x.value).map(x=>x.value),['123456789','987654321'],'Search keeps the current selection visible and finds the full Feishu chat ID')
assert.equal(nodes.targetSessionPicker.value,'123456789','Filtering does not hide the active selection')
for(const sourcePlatform of ['aiocqhttp','larkx']) for(const targetPlatform of ['aiocqhttp','larkx']) {
 const source={platform:sourcePlatform,instance:sourcePlatform==='larkx'?'default':'qq_bridge',account:'111',messageType:'GroupMessage',session:'100'}
 const target={platform:targetPlatform,instance:targetPlatform==='larkx'?'default':'qq_bridge',account:'222',messageType:'FriendMessage',session:'200'}
 context.fixtureSource=source;context.fixtureTarget=target
 const route=vm.runInContext('buildRouteFromEndpoints(fixtureSource,fixtureTarget)',context)
 assert.equal(route.source_platform,sourcePlatform)
 assert.equal(route.source_account_id,sourcePlatform==='aiocqhttp'?'111':'')
 assert.equal(route.target_account_id,targetPlatform==='aiocqhttp'?'222':'')
 assert.equal(route.target_umo,`${targetPlatform==='larkx'?'larkx':'qq_bridge'}:FriendMessage:200`)
 assert.equal(route.include_self,false)
 assert.equal(vm.runInContext('buildRouteFromEndpoints(fixtureSource,fixtureTarget).include_self',context),false)
 assert.deepEqual(Array.from(route.content_types),contentTypeValues,'Every platform pair defaults to all supported content types')
}
const sourceEndpoint={platform:'aiocqhttp',instance:'qq_bridge',account:'111',messageType:'GroupMessage',session:'100'}
const targetEndpoint={platform:'larkx',instance:'default',account:'',messageType:'GroupMessage',session:'123456789'}
context.fixtureSource=sourceEndpoint;context.fixtureTarget=targetEndpoint
const defaultRoute=vm.runInContext('buildRouteFromEndpoints(fixtureSource,fixtureTarget)',context)
const customRoute=vm.runInContext("buildRouteFromEndpoints(fixtureSource,fixtureTarget,true,['audio','image','video','audio','unsupported'])",context)
assert.deepEqual(Array.from(customRoute.content_types),['image','video','audio'],'Saved selections are unique, supported and ordered as the controls')
assert.equal(customRoute.include_self,true)
context.fixtureDefaultRoute=defaultRoute;context.fixtureCustomRoute=customRoute
assert.equal(vm.runInContext('routeContentTypeLabel(fixtureDefaultRoute)',context),'文字、表情包、图片、视频、语音、文件')
assert.equal(vm.runInContext('routeContentTypeLabel({})',context),'文字、表情包、图片、视频、语音、文件','Legacy routes without a selection retain the all-types default')
assert.equal(vm.runInContext('routeContentTypeLabel(fixtureCustomRoute)',context),'图片、视频、语音')
assert.equal(vm.runInContext('routeContentTypeLabel({content_types:[]})',context),'未选择','An explicit empty selection must not turn into all types')
assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext('routeEndpoints(fixtureCustomRoute)',context))),[sourceEndpoint,targetEndpoint],'Editing restores each platform, account and chat endpoint')
const reverseRoute=vm.runInContext("buildRouteFromEndpoints(fixtureTarget,fixtureSource,true,['image','video','audio'])",context)
const differentAccountRoute={...reverseRoute,target_account_id:'222'}
context.fixtureReverseRoute=reverseRoute;context.fixtureDifferentAccountRoute=differentAccountRoute
vm.runInContext('state.rules=[fixtureCustomRoute,fixtureDifferentAccountRoute,fixtureReverseRoute]',context)
assert.equal(vm.runInContext('routeReciprocalIndex(0)',context),2,'Only the reverse route with the same QQ account pairs with a rule')
assert.equal(vm.runInContext('routeReciprocalIndex(2)',context),0,'Reciprocal detection works in both directions')
assert.equal(vm.runInContext('routeReciprocalIndex(1)',context),-1,'A different destination account is an independent rule')
assert.equal(vm.runInContext('routeReciprocalIndex(9)',context),-1)
vm.runInContext('state.mappingEditingIndices=[0,2];populateMappingEditor()',context)
assert.deepEqual(contentTypeInputs.filter(input=>input.checked).map(input=>input.value),['image','video','audio'],'The editor restores the saved type selection')
assert.equal(nodes.mappingIncludeSelf.checked,true)
assert.equal(nodes.mappingDirection.value,'both')
assert.equal(nodes.mappingDirection.disabled,false,'Direction remains editable on every saved mapping')
assert.equal(nodes.mappingDialogTitle.textContent,'编辑双向映射')
assert.equal(nodes.submitMapping.textContent,'保存修改')
vm.runInContext('state.rules=[{...fixtureDefaultRoute}];delete state.rules[0].content_types;state.mappingEditingIndices=[0];populateMappingEditor()',context)
assert.deepEqual(contentTypeInputs.filter(input=>input.checked).map(input=>input.value),contentTypeValues,'Editing an older rule shows its all-types default')
assert.equal(nodes.mappingDirection.value,'forward')
assert.equal(nodes.mappingDialogTitle.textContent,'编辑转发规则')
assert.notEqual(vm.runInContext("routeIdentity({source_platform:'larkx',source_session_id:'100',target_umo:'qq_bridge:GroupMessage:100'})",context),vm.runInContext("routeIdentity({source_platform:'larkx',source_session_id:'100',target_umo:'qq_bridge:GroupMessage:200'})",context))
assert.equal(vm.runInContext("endpointIdentity({platform:'larkx',instance:'default',account:'',messageType:'GroupMessage',session:'100'})===endpointIdentity({platform:'larkx',instance:'default',account:'irrelevant',messageType:'GroupMessage',session:'100'})",context),true)
context.qqRuntime={runtime:{qq:{state:'connected',connected:true,identity_verified:true,accounts:[{platform_id:'qq_bridge',user_id:'111',nickname:'First',identity_verified:true,connected:true,groups:[{group_id:'100'}]},{platform_id:'qq_bridge',user_id:'222',nickname:'Second',identity_verified:true,connected:true,groups:[{group_id:'200'}]}]},snowluma:{accounts:[{uin:'111',nickname:'First'},{uin:'222',nickname:'Second'}],url:'http://127.0.0.1:5099/'}}}
vm.runInContext('renderQqRuntime(qqRuntime)',context)
assert.equal(nodes.qqAccountsList.children.length,2,'Both locally connected QQ identities are shown separately')
assert.match(nodes.qqAccountsList.children[0].children[0].textContent,/First · 111/)
assert.match(nodes.qqAccountsList.children[1].children[0].textContent,/Second · 222/)
assert.equal(nodes.openSnowLumaLocal.href,'http://127.0.0.1:5099/')
const html=fs.readFileSync(path.join(__dirname,'../workbench/index.html'),'utf8')
assert.match(html,/id="bridgeToggle"/,'The global forwarding control exists')
assert.match(html,/mappingIncludeSelf" type="checkbox">/,'Per-rule manual-message switch defaults off')
assert.match(html,/<fieldset id="contentTypeOptions"[^>]*>\s*<legend>转发内容类型<\/legend>/,'Content types are exposed as one labelled control group')
const contentCheckboxes=[...html.matchAll(/<input type="checkbox" name="contentType" value="([^"]+)" checked>/g)].map(match=>match[1])
assert.deepEqual(contentCheckboxes,contentTypeValues,'New rules visibly default to the six supported message categories')
for(const label of ['文字','表情包','图片','视频','语音','文件']) assert.match(html,new RegExp(`checked>${label}<\\/label>`))
assert.doesNotMatch(html,/list="(?:source|target)SessionOptions"/,'ID fields do not open ID-first browser autocomplete suggestions')
const app = fs.readFileSync(path.join(__dirname,'../workbench/app.js'),'utf8')
assert.match(app,/data-toggle-self/,'Saved routes can toggle self-authored messages')
assert.match(fs.readFileSync(path.join(__dirname,'../workbench/app.js'),'utf8'),/postAction\('\/api\/bridge\/enable'/,'The global switch calls the local bridge control')

async function testMappingSaves() {
  for (const id of html.matchAll(/\bid="([^"]+)"/g)) nodes[id[1]] ||= element()
  let stored = [defaultRoute, differentAccountRoute]
  let reject = false
  const posted = []
  let respondDiscovery
  const discovery = new Promise(resolve => { respondDiscovery = resolve })
  const fixtureDiscovery = vm.runInContext('state.mappingDiscovery', context)
  context.fetch = async (path, options) => {
    if (path === '/api/mapping/discovery') return {ok:true,json:async () => discovery}
    if (path === '/api/status') return {ok:true,json:async () => ({})}
    assert.equal(path, '/api/rules')
    if (options.method === 'POST') {
      const body = JSON.parse(options.body)
      posted.push(body.rules)
      if (reject) return {ok:false,status:400,json:async () => ({error:'模拟验证失败'})}
      stored = body.rules
    }
    return {ok:true,json:async () => ({rules:structuredClone(stored)})}
  }
  vm.runInContext('renderStatus = () => {}; state.mappingDiscovery = null; bindEvents()', context)
  await vm.runInContext('refresh(true)', context)
  const dispatch = (id, type, fields = {}) => nodes[id].listeners[type]({preventDefault(){},...fields})
  const rowClick = (action, key, index) => dispatch('rulesTable','click',{target:{closest(selector){return selector === action ? {dataset:{[key]:String(index)}} : null}}})
  await rowClick('[data-edit-route]', 'editRoute', 0)
  assert.equal(nodes.mappingDirection.value,'forward')
  assert.equal(nodes.sourceAccount.value,'111','Editor restores the saved account before discovery completes')
  assert.equal(nodes.mappingIncludeSelf.checked,false)
  nodes.mappingDirection.value = 'both'
  dispatch('mappingDirection','change')
  nodes.mappingIncludeSelf.checked = true
  dispatch('mappingIncludeSelf','change')
  const pendingDiscovery = vm.runInContext('state.mappingDiscoveryPromise', context)
  respondDiscovery(fixtureDiscovery)
  await pendingDiscovery
  assert.equal(nodes.mappingDirection.value,'both','Discovery cannot reset user choices')
  assert.equal(nodes.mappingIncludeSelf.checked,true)

  reject = true
  await dispatch('mappingForm','submit')
  assert.equal(nodes.mappingDialog.open,true)
  assert.match(nodes.dialogError.textContent,/模拟验证失败/)
  assert.equal(vm.runInContext('state.rules.length',context),2,'A rejected save leaves the table unchanged')
  assert.equal(stored[0].include_self,false)
  reject = false
  await dispatch('mappingForm','submit')
  assert.equal(nodes.mappingDialog.open,false)
  assert.equal(stored.length,3)
  assert.deepEqual([stored[0].include_self, stored[2].include_self],[true,true])
  assert.equal(stored[2].target_account_id,'111')
  assert.equal(stored[2].source_session_id,'123456789')
  assert.deepEqual(stored[1],JSON.parse(JSON.stringify(differentAccountRoute)))
  await vm.runInContext('refresh(true)',context)
  await rowClick('[data-edit-route]', 'editRoute', 0)
  assert.equal(nodes.mappingDirection.value,'both')
  nodes.mappingDirection.value = 'forward'
  dispatch('mappingDirection','change')
  await dispatch('mappingForm','submit')
  assert.equal(stored.length,2,'Switching to one way removes only the matching reverse rule')
  assert.equal(stored[1].target_account_id,'222')

  await rowClick('[data-remove-route]', 'removeRoute', 1)
  await rowClick('[data-toggle-self]', 'toggleSelf', 0)
  assert.equal(posted.at(-1).length,2,'An immediate self toggle must not save an unrelated staged deletion')
  assert.equal(stored[0].include_self,false)
  assert.equal(vm.runInContext('state.rules.length',context),1,'A staged deletion is retained in the table')
  await vm.runInContext('refresh(true)',context)
  assert.equal(vm.runInContext('state.rules[0].include_self',context),false,'Reload reflects the saved self flag')
  reject = true
  await rowClick('[data-toggle-self]', 'toggleSelf', 0)
  assert.equal(vm.runInContext('state.rules[0].include_self',context),false)
  assert.match(nodes.toast.textContent,/本人消息设置未保存/)
}

testMappingSaves().then(() => console.log('Workbench editor, direction persistence, self flags and save errors passed.')).catch(error => {
  console.error(error)
  process.exitCode = 1
})
