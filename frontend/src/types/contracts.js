/** @typedef {{id:number,code:string,amount:number,distance:number,holiday:boolean,label:0|1|null,note:string}} Claim */
/** @typedef {{id:number,name:string,threshold:number,state:string,result:object}} Training */
/** @typedef {{id:number,claim_id:number,training_id:number,score:number,decision:string,status:string}} Review */
/** @typedef {{payment_no:string,kind:'payment'|'reversal',original_no:string|null,amount_cents:number,status:string,allocations:{claim_code:string,amount_cents:number}[]}} Payment */
/** @typedef {{id:number,status:'received'|'completed'|'failed',source:string,errors:object[],summary:object,recon_snapshot_id:number|null,row_count:number}} PaymentBatch */
/** @typedef {{version:number|null,totals:object,claims:object[],checksum?:string}} Reconciliation */
