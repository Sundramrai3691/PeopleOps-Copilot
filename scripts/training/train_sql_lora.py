"""Local QLoRA pilot with frozen source-labelled paired evaluation.

No network model loads, no production DBs, no automatic model promotion.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import random
import time

from sqlglot import exp, parse
from sql_agent.training_dataset import execute_read


def messages(row):
    schema = ';\n'.join(n.sql(dialect='sqlite') for n in parse(row['sql_context'], read='sqlite')
                        if isinstance(n, exp.Create))
    return [{'role':'system', 'content':'Generate one read-only SQLite query. Return only a JSON object with a sql string. Use the supplied schema. Do not invent columns.'},
            {'role':'user', 'content':json.dumps({'question':row['sql_prompt'], 'schema':schema})}]


def wilson(correct, total):
    z = 1.96
    p = correct / total
    center = (p + z*z/(2*total))/(1+z*z/total)
    width = z*math.sqrt(p*(1-p)/total + z*z/(4*total*total))/(1+z*z/total)
    return [center-width, center+width]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--model', required=True)
    p.add_argument('--dataset', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    torch.set_num_threads(4)
    random.seed(42)
    torch.manual_seed(42)
    if not torch.cuda.is_available():
        raise RuntimeError('CUDA required; no silent CPU training fallback')
    data = json.loads(args.dataset.read_text())
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {'dataset_sha256':hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
        'model_path':args.model, 'model_file_sha256':{
            path.name:hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
            for path in Path(args.model).glob('*') if path.is_file() and path.suffix in {'.json','.safetensors'}},
        'seed':42, 'rank':16, 'alpha':32, 'learning_rate':0.0001,
        'epochs':1, 'max_sequence':1024, 'gradient_accumulation':8,
        'evaluation_scope':'source-labelled SQL execution on one fixture per query; not human business correctness or deployed Agent accuracy',
        'rag':'disabled identically in both conditions', 'generation_max_new_tokens':192,
        'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'token_accounting':'Input excludes left padding; output includes first EOS and excludes padding after EOS. Local generation, no API billing.',
        'gpu':torch.cuda.get_device_name(0)}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True, trust_remote_code=False)
    tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(args.model, local_files_only=True, trust_remote_code=False,
        torch_dtype=torch.bfloat16, device_map={'':0}, attn_implementation='sdpa',
        quantization_config=BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type='nf4',
            bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16))
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=.05,
        target_modules=['q_proj','k_proj','v_proj','o_proj'], task_type='CAUSAL_LM'))
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(json.dumps({'trainable_parameters':trainable}), flush=True)

    def evaluate(name):
        model.eval()
        tokenizer.padding_side = 'left'
        records = []
        for offset in range(0, len(data['splits']['test']), 4):
            rows = data['splits']['test'][offset:offset+4]
            texts = [tokenizer.apply_chat_template(messages(r), tokenize=False, add_generation_prompt=True) for r in rows]
            inputs = tokenizer(texts, padding=True, return_tensors='pt').to('cuda')
            if inputs.input_ids.shape[1] > 1024:
                raise ValueError('test prompt exceeds frozen token budget')
            tick = time.perf_counter()
            with torch.inference_mode():
                output = model.generate(**inputs, do_sample=False, max_new_tokens=192,
                    pad_token_id=tokenizer.pad_token_id, use_cache=True)
            torch.cuda.synchronize()
            elapsed = time.perf_counter()-tick
            decoded = tokenizer.batch_decode(output[:,inputs.input_ids.shape[1]:], skip_special_tokens=True)
            for index, (row, text) in enumerate(zip(rows, decoded)):
                generated = output[index, inputs.input_ids.shape[1]:].tolist()
                eos = model.generation_config.eos_token_id
                eos_ids = set(eos if isinstance(eos, list) else [eos])
                output_tokens = next((i+1 for i, token in enumerate(generated) if token in eos_ids), len(generated))
                input_tokens = int(inputs.attention_mask[index].sum())
                rec = {'id':row['id'], 'domain':row['domain'], 'response':text, 'correct':False,
                       'input_tokens':input_tokens, 'output_tokens':output_tokens,
                       'total_tokens':input_tokens+output_tokens,
                       'batch_seconds':elapsed, 'batch_size':len(rows)}
                try:
                    sql = json.loads(text)['sql']
                    result = execute_read(row['sql_context'], sql)
                    expected = row['expected_rows']
                    ordered = bool(parse(row['sql'], read='sqlite')[0].args.get('order'))
                    rec['correct'] = result == expected if ordered else Counter(map(tuple,result)) == Counter(map(tuple,expected))
                    rec['execution_succeeded'] = True
                except Exception as exc:
                    rec.update(execution_succeeded=False, error_type=type(exc).__name__)
                records.append(rec)
            (args.output/(name+'.json')).write_text(json.dumps(records,indent=2))
            print(json.dumps({'phase':name,'done':len(records),'correct':sum(r['correct'] for r in records)}),flush=True)
        return records

    with model.disable_adapter():
        baseline = evaluate('baseline')
    model.train()
    model.config.use_cache = False
    tokenizer.padding_side = 'right'
    optimizer = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=.0001)
    rows = list(data['splits']['train'])
    random.Random(42).shuffle(rows)
    optimizer.zero_grad()
    used, skipped, losses = 0, 0, []
    training_tokens, supervised_tokens = 0, 0
    torch.cuda.reset_peak_memory_stats()
    tick = time.perf_counter()
    for row in rows:
        prompt = tokenizer.apply_chat_template(messages(row), tokenize=False, add_generation_prompt=True)
        full = tokenizer.apply_chat_template(messages(row)+[{'role':'assistant','content':json.dumps({'sql':row['sql']})}], tokenize=False)
        ids = tokenizer(full, add_special_tokens=False, return_tensors='pt').input_ids.to('cuda')
        prefix = tokenizer(prompt, add_special_tokens=False).input_ids
        if ids.shape[1] > 1024:
            skipped += 1
            continue
        if ids[0,:len(prefix)].tolist() != prefix:
            raise ValueError('chat template prefix mismatch')
        labels = ids.clone()
        labels[:,:len(prefix)] = -100
        training_tokens += ids.numel()
        supervised_tokens += int((labels != -100).sum())
        loss = model(input_ids=ids, attention_mask=torch.ones_like(ids), labels=labels).loss
        if not torch.isfinite(loss):
            raise ValueError('non-finite training loss')
        (loss/8).backward()
        used += 1
        losses.append(float(loss.detach()))
        if used % 8 == 0:
            torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
            optimizer.step()
            optimizer.zero_grad()
        if used % 32 == 0:
            print(json.dumps({'phase':'training','samples':used,'loss':sum(losses[-32:])/32}),flush=True)
    if used % 8:
        for parameter in model.parameters():
            if parameter.grad is not None:
                parameter.grad.mul_(8/(used % 8))
        torch.nn.utils.clip_grad_norm_(model.parameters(),1.0)
        optimizer.step()
    model.save_pretrained(args.output/'adapter')
    tokenizer.save_pretrained(args.output/'adapter')
    training = {'samples':used,'skipped_overlength':skipped,'seconds':time.perf_counter()-tick,
                'training_tokens':training_tokens,'supervised_tokens':supervised_tokens,
                'trainable_parameters':trainable,'peak_allocated_bytes':torch.cuda.max_memory_allocated(), 'losses':losses}
    (args.output/'training.json').write_text(json.dumps(training,indent=2))
    after = evaluate('adapter')
    b, a = sum(r['correct'] for r in baseline), sum(r['correct'] for r in after)
    summary = {'n':len(after),'baseline_correct':b,'adapter_correct':a,
        'baseline_wilson95':wilson(b,len(after)), 'adapter_wilson95':wilson(a,len(after)),
        'fixed':sum(not x['correct'] and y['correct'] for x,y in zip(baseline,after)),
        'regressed':sum(x['correct'] and not y['correct'] for x,y in zip(baseline,after)),
        'caution':'Wilson intervals are descriptive; domain clustering and single-fixture oracle limit generalization.'}
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2))
    print(json.dumps(summary),flush=True)


if __name__ == '__main__':
    main()
