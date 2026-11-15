const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
function element() {
  return {value:'',textContent:'',hidden:false,children:[],className:'',href:'',title:'',disabled:false,attributes:{},setAttribute(name,value){this.attributes[name]=value},replaceChildren(...children){this.children=children;this.value=children[0]?.value||''},append(...children){this.children.push(...children)}}
}
const nodes={toast:element()}
const suffixes=['Platform','Instance','InstanceOptions','Account','AccountLabel','AccountHelp','MessageType','Session','SessionOptions','SessionLabel','SessionHelp','SessionSearch','SessionPicker']
for(const side of ['source','target']) for(const suffix of suffixes) nodes[side+suffix]=element()
for(const id of ['qqAccountSummary','qqIdentityState','qqAccountsList','qqForwardActivity','qqState','qqDot','openSnowLumaLocal','qqOneBotAddress','launchLocalQq','bridgeToggle']) nodes[id]=element()
const context=vm.createContext({URL,URLSearchParams,location:{href:'http://127.0.0.1:18766/',search:'',replace(){}},localStorage:{getItem(){return null}},document:{documentElement:{dataset:{}},querySelector(selector){return nodes[selector.slice(1)]||null},createElement:element}})
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
}
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
assert.doesNotMatch(html,/list="(?:source|target)SessionOptions"/,'ID fields do not open ID-first browser autocomplete suggestions')
const app = fs.readFileSync(path.join(__dirname,'../workbench/app.js'),'utf8')
assert.match(app,/data-toggle-self/,'Saved routes can toggle self-authored messages')
assert.match(fs.readFileSync(path.join(__dirname,'../workbench/app.js'),'utf8'),/postAction\('\/api\/bridge\/enable'/,'The global switch calls the local bridge control')
console.log('Name-first group labels, duplicate-name disambiguation, ID search, selection persistence and four platform pairs passed.')
