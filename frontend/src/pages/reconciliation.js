import {state} from '../stores/session.js';import {esc} from '../utils/html.js';import {badge} from '../components/common/status.js';import {field} from '../components/common/fields.js';
const money=c=>(c/100).toFixed(2);
const labels={settled:'已结清',unsettled:'未结清',overpaid:'超付',held:'冲正挂起'};
const batchLabels={received:'已接收',completed:'已完成',failed:'失败'};
function basisRows(c){return c.basis.map(b=>`<tr><td>${esc(b.payment_no)}</td><td>${b.kind==='reversal'?'冲正':'支付'}</td><td>${esc(b.original_no||'—')}</td><td>${b.status==='held'?'挂起':'已入账'}</td><td>${money(b.allocated_cents)}</td><td>#${esc(b.batch_id??'—')}</td></tr>`).join('')||'<tr><td colspan="6">暂无流水依据</td></tr>'}

export function render(){
 const r=state.reconciliation||{claims:[],totals:{}};
 const t=r.totals||{};
 const batches=state.batches||[];
 const filtered=r.claims.filter(c=>!state.reconFilter||c.code.includes(state.reconFilter)||labels[c.status].includes(state.reconFilter));
 return `<section class="panel"><div class="result-head"><h2>支付流水对账</h2><span class="tag">依据版本 ${r.version?('#'+r.version):'未生成'}</span></div>
 <p class="notice">报销标记“正常”不代表款项已结清。只有已结清的正常样本进入训练；未结清、超付或冲正挂起的正常样本暂不入训，异常标签照常保留。冲正引用原支付，可先于支付到达并挂起，原支付到达后自动重算受影响票据。金额按分守恒。</p>
 <div class="metrics"><div><strong>${r.version?money(t.net_cents):'—'}</strong>到账净额 / 元</div><div><strong>${r.version?money(t.allocated_cents):'—'}</strong>逐票分摊 / 元</div><div><strong>${t.settled??0}</strong>已结清票据</div><div><strong>${(t.unsettled??0)+(t.overpaid??0)+(t.held_claims??0)}</strong>待处理票据</div></div>
 <p class="eyebrow">依据版本 ${r.version?('#'+r.version+' · '+esc(r.created)+' · 校验 '+esc((r.checksum||'').slice(0,12))):'尚未导入流水'} · 挂起流水 ${t.held_payments??0} 笔</p>
 <h3>票据差额与流水依据</h3><form id="recon-filter" class="inline"><input name="q" placeholder="按票据编号或状态筛选，如 HX-1115-001 / 超付" value="${esc(state.reconFilter||'')}"><button>筛选</button></form>
 <div class="scroll"><table><thead><tr><th>票据</th><th>报销 / 元</th><th>已入账净额 / 元</th><th>差额 / 元</th><th>状态</th><th>流水依据</th></tr></thead><tbody>
 ${filtered.map(c=>`<tr><td>${esc(c.code)}</td><td>${money(c.amount_cents)}</td><td>${money(c.paid_cents)}</td><td>${money(c.difference_cents)}</td><td>${badge(labels[c.status]||c.status)}</td><td><details><summary>${c.basis.length} 笔</summary><div class="scroll"><table><thead><tr><th>流水编号</th><th>类型</th><th>引用原支付</th><th>入账</th><th>分摊 / 元</th><th>批次</th></tr></thead><tbody>${basisRows(c)}</tbody></table></div></details></td></tr>`).join('')||'<tr><td colspan="6">没有匹配的票据。</td></tr>'}
 </tbody></table></div>
 <div class="result-head"><h3>导入批次</h3><button id="refresh-batches">刷新批次</button></div>
 ${batches.length?`<div class="scroll"><table><thead><tr><th>批次</th><th>状态</th><th>来源</th><th>行数</th><th>新入账</th><th>跳过重复</th><th>挂起</th><th>依据版本</th><th>错误</th><th></th></tr></thead><tbody>
 ${batches.map(b=>`<tr><td>#${b.id}</td><td>${badge(batchLabels[b.status]||b.status)}</td><td>${esc(b.source)}</td><td>${b.row_count}</td><td>${b.summary?.applied??'—'}</td><td>${b.summary?.skipped??'—'}</td><td>${(b.summary?.held_created??0)+(b.summary?.held_resolved??0)}</td><td>${b.recon_snapshot_id?('#'+b.recon_snapshot_id):'—'}</td><td>${esc((b.errors||[]).map(e=>e.message).join('；')||'—')}</td><td>${b.status!=='completed'?`<button data-retry="${b.id}">按原批恢复</button>`:''}</td></tr>`).join('')}
 </tbody></table></div>`:'<p>尚未导入支付流水。</p>'}
 </section>
 <aside class="side"><h2>导入支付流水</h2><form id="payment">
 ${field('来源说明','source','text','value="银行流水导入"')}
 <label>流水 JSON（数组或每行一个对象）<textarea name="payload" rows="12" required placeholder='[{"payment_no":"P-1","kind":"payment","amount":100.00,"allocations":[{"claim_code":"HX-1115-001","amount":100.00}]}]'></textarea></label>
 <p class="eyebrow">字段：payment_no 唯一编号 · kind payment/reversal · original_no 冲正引用的原支付 · amount 元 · allocations 逐票分摊（冲正为负数）。分摊合计必须等于流水金额；重复编号内容不同会被拒绝。</p>
 <button type="button" id="fill-example">填入示例</button> <button class="primary">导入并对账</button>
 </form><p>导入状态分接收、完成、失败。失败批次保留原始行，可接着原批恢复；已完成批次重试不会重复计款。</p></aside>`}
