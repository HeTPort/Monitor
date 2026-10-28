#!/usr/bin/env python3
"""压测用例执行、证据采集、续跑与中文汇总；不调压、不锁频、不伪造压测时长。"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'): sys.stderr.reconfigure(encoding='utf-8')
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.artifact_store import atomic_write_json, sha256_file
from src.config_loader import ProfileConfig, validate_workload_config
from src.verification_contract import verification_requirement, verification_issues

TZ=timezone(timedelta(hours=8))


def now(): return datetime.now(TZ).isoformat(timespec='seconds')
def load(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def finite(value): return type(value) in (int,float) and math.isfinite(value) and value>=0


def validate_plan(plan):
    if not isinstance(plan,dict) or plan.get('schema_version')!=1:
        raise ValueError('计划须包含有效的 schema_version')
    stages=plan.get('stages')
    if stages is not None:
        if 'days' in plan: raise ValueError('stages 与 days 不能同时声明')
        if not isinstance(stages,list) or not 1<=len(stages)<=1000:
            raise ValueError('stages 必须为非空阶段列表，最多 1000 项')
        if any(not isinstance(s,dict) or type(s.get('stage')) is not int for s in stages) or [s['stage'] for s in stages]!=list(range(1,len(stages)+1)):
            raise ValueError('stage 必须从 1 开始连续编号')
    else:
        stages=plan.get('days')
        if not isinstance(stages,list) or len(stages)!=7 or any(not isinstance(s,dict) or type(s.get('day')) is not int for s in stages) or [s['day'] for s in stages]!=list(range(1,8)):
            raise ValueError('旧格式计划须包含完整顺序编号；新计划请使用 stages')
    for stage in stages:
        if type(stage.get('cycles',1)) is not int or not 1<=stage.get('cycles',1)<=10000: raise ValueError('cycles 必须为 1..10000')
        if not isinstance(stage.get('profiles'),list) or not stage['profiles'] or any(not isinstance(p,str) or not p for p in stage['profiles']): raise ValueError('每阶段须列出 profile')
    return plan


def plan_stages(plan):
    return plan.get('stages',plan.get('days',[]))


def monitor_command():
    return [sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(ROOT/'main.py')]


def board_config(path):
    board=load(path)
    for key in ('board_id','device','pc_serial','platform'):
        if not isinstance(board.get(key),str) or not board[key].strip(): raise ValueError(f'板卡配置缺少 {key}')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,24}',board['board_id']): raise ValueError('board_id 仅支持不超过 24 字符的字母数字及 ._-')
    if board.get('transport','hdc') not in ('hdc','adb'): raise ValueError('transport 应为 hdc 或 adb')
    if not isinstance(board.get('available_targets'),list) or set(board['available_targets'])-{'cpu','gpu','npu'}: raise ValueError('available_targets 应显式声明 cpu/gpu/npu')
    if type(board.get('npu_delegate_ready',False)) is not bool: raise ValueError('npu_delegate_ready 必须为布尔值')
    for kind in ('golden_by_profile','baseline_by_profile'):
        if not isinstance(board.get(kind,{}),dict): raise ValueError(f'{kind} 应为映射')
    if set(board.get('golden_by_profile',{})) & set(board.get('baseline_by_profile',{})): raise ValueError('同一 profile 的 golden/baseline 互斥')
    return board


def resolve_profile(name,board_path,paths=None):
    if paths is not None:
        from src.cli_commands import load_profile
        raw=Path(name)
        selection=str((board_path.parent/raw).resolve()) if not raw.is_absolute() and raw.suffix in (".json",".yaml",".yml") else name
        profile=load_profile(paths,selection)
        config_path=paths.resolve_input(profile.workload["config"],owner=profile.source_path)
        return profile,validate_workload_config(config_path,profile.target),config_path
    raw=Path(name)
    path=(raw if raw.is_absolute() else board_path.parent/raw) if raw.suffix in ('.json','.yaml','.yml') else ROOT/'config/profiles'/f'{name}.yaml'
    profile=ProfileConfig.from_file(path)
    value=Path(profile.workload['config'])
    config_path=value if value.is_absolute() else profile.source_path.parent/value
    config=validate_workload_config(config_path,profile.target)
    return profile,config,config_path.resolve()


def ledger_rows(path):
    if not path.exists(): return []
    rows=[]
    for number,line in enumerate(path.read_text(encoding='utf-8').splitlines(),1):
        if line.strip():
            try: rows.append(json.loads(line))
            except ValueError as exc: raise ValueError(f'ledger 第 {number} 行损坏，停止汇总以保留证据') from exc
    return rows


def append_ledger(path,row):
    with path.open('a',encoding='utf-8',newline='\n') as stream:
        stream.write(json.dumps(row,ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())


def measurements(summary,profile,config,verdict,collected):
    units=summary.get({'cpu':'batch_count','gpu':'frame_count','npu':'inference_count'}[profile.target])
    framework=(profile.target=='gpu' and config.get('api')=='null') or (profile.target=='npu' and config.get('backend') in ('null','reference_cpu')) or (profile.target=='cpu' and config.get('backend')=='null')
    duration=summary.get('duration_s',summary.get('duration'))
    observed=float(duration) if finite(duration) and type(units) is int and units>0 and not framework else None
    issues=verification_issues(summary,verification_requirement(profile.target,config))
    return {'measured_workload_s':observed,'accepted_load_s':observed if collected and verdict=='PASS' else None,
            'scope':'框架检查' if framework else '持续负载', 'correctness_checked':config.get('verify_mode')!='none' and not issues,
            'verification_issues':issues,'work_units':units,'native_duration_s':duration if finite(duration) else None}


class Runner:
    def __init__(self,args):
        self.args=args;self.board_path=args.board.resolve(strict=True);self.board=board_config(self.board_path)
        self.plan_path=args.plan.resolve(strict=True);self.plan=validate_plan(load(self.plan_path))
        if not 1<=args.day<=len(plan_stages(self.plan)): raise ValueError('阶段编号不在计划范围内')
        self.paths=getattr(args,'runtime_paths',None)
        self.campaign=args.campaign.resolve();self.ledger=self.campaign/'ledger.jsonl'
        self.output=self.campaign/'runs'/self.board['board_id'];self.state=self.campaign/'state'/self.board['board_id']
        self.history=ledger_rows(self.ledger)
        self.identity=hashlib.sha256((sha256_file(self.plan_path)+sha256_file(self.board_path)).encode()).hexdigest()
        self.global_args=monitor_command()+['--json','--transport',self.board.get('transport','hdc'),
            '--device',self.board['device'],'--pc-serial',self.board['pc_serial'],'--output-dir',str(self.output),'--state-dir',str(self.state)]
        if self.paths is not None:
            self.global_args+=['--device-root',str(self.paths.device_root)]
            if self.paths.config_dir: self.global_args+=['--config-dir',str(self.paths.config_dir)]
        for option in ('hdc_bin','adb_bin'):
            value=getattr(args,option,None)
            if value:self.global_args+=['--'+option.replace('_','-'),value]
        if self.board.get('device_uart'): self.global_args+=['--device-uart',self.board['device_uart']]
        if self.board.get('baudrate'): self.global_args+=['--baudrate',str(self.board['baudrate'])]
        self.test_id=f'S-{self.board["board_id"]}-D{args.day}-{self.identity[:6]}'

    def reference(self,name):
        if name in self.board.get('golden_by_profile',{}):
            value=Path(self.board['golden_by_profile'][name]);value=value if value.is_absolute() else self.board_path.parent/value
            return ['--golden',str(value.resolve(strict=True))]
        if name in self.board.get('baseline_by_profile',{}): return ['--baseline',str(self.board['baseline_by_profile'][name])]
        return []

    def invoke(self,argv,folder,stage,timeout):
        command=self.global_args+list(map(str,argv))
        print(stage+': '+json.dumps(command,ensure_ascii=False),flush=True)
        if self.args.dry_run: return 0,{}
        try:
            completed=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
            (folder/f'{stage}.stdout.log').write_text(completed.stdout,encoding='utf-8')
            (folder/f'{stage}.stderr.log').write_text(completed.stderr,encoding='utf-8')
            try: body=json.loads(completed.stdout)
            except ValueError: body={}
            atomic_write_json(folder/f'{stage}.command.json',{'argv':command,'exit_code':completed.returncode,'finished_at':now()})
            return completed.returncode,body if isinstance(body,dict) else {}
        except subprocess.TimeoutExpired as exc:
            atomic_write_json(folder/f'{stage}.timeout.json',{'argv':command,'timeout_s':timeout,'time':now(),'device_state':'待人工确认进程已结束'})
            raise RuntimeError(f'{stage} 主机超时；必须检查设备进程和现场，不能把等待时间算作有效压测') from exc

    def execute(self,name,cycle):
        key=hashlib.sha256(f'{self.identity}:{self.board["board_id"]}:{self.args.day}:{name}:{cycle}'.encode()).hexdigest()
        prior=[r for r in self.history if r.get('execution_key')==key]
        if any(r.get('state')=='COMPLETED' and r.get('collection_verified') is True for r in prior):
            print(f'续跑跳过已完成且证据完整项：{name} cycle={cycle}');return True
        if prior and not self.args.retry_failed:
            print(f'已存在失败/跳过记录：{name}；重试请显式 --retry-failed')
            return not any(r.get('state') in ('FAILED','INTERRUPTED') for r in prior)
        profile,config,config_path=resolve_profile(name,self.board_path,self.paths)
        reference=self.reference(name)
        reason=None
        if profile.target not in self.board['available_targets']: reason='板卡未声明支持该 target'
        elif profile.platform!=self.board['platform']: reason='profile 平台与实际板卡配置不一致，须先适配'
        elif profile.target=='npu' and config.get('backend')=='tflite_delegate' and not self.board.get('npu_delegate_ready',False): reason='尚未准备厂商 NPU runtime/delegate'
        elif config.get('verify_mode')=='golden-image' and not reference: reason='严格 GPU 正确性用例缺少本 profile 的 golden/baseline'
        elif profile.target=='npu' and profile.raw.get('purpose')=='qualification' and not reference: reason='NPU 资格化缺少已批准外部基准'
        row={'schema_version':1,'execution_key':key,'record_id':uuid.uuid4().hex,'day':self.args.day,'cycle':cycle,
             'board_id':self.board['board_id'],'profile':profile.name,'target':profile.target,'started_at':now(),
             'profile_sha256':profile.fingerprint,'workload_config_sha256':sha256_file(config_path),'plan_sha256':sha256_file(self.plan_path),
             'board_config_sha256':sha256_file(self.board_path),'state':'SKIPPED' if reason else 'RUNNING','skip_reason':reason,
             'collection_verified':False,'frequency_note':self.board.get('frequency_note','未锁频/待确认')}
        if reason:
            print(f'SKIP {name}: {reason}')
            if not self.args.dry_run: append_ledger(self.ledger,row|{'finished_at':now()})
            return True
        attempt=f'A{self.args.day}{cycle}-{profile.target}-{uuid.uuid4().hex[:8]}'
        folder=self.campaign/'operations'/self.board['board_id']/attempt
        if not self.args.dry_run: folder.mkdir(parents=True,exist_ok=False)
        row.update(test_id=self.test_id,attempt_id=attempt,operations_dir=str(folder),collection_verified=False)
        overall=max(float(config.get('timeout',300))+30,120)
        overall=max(overall,float(profile.raw.get('runtime_budgets',{}).get('overall_timeout_s',0)))
        run_dir=self.output/self.test_id/attempt
        try:
            code,_=self.invoke(['validate','--profile',str(profile.source_path)],folder,'01-validate',60)
            if code: raise RuntimeError('配置/资产校验失败')
            if self.args.prepare:
                code,_=self.invoke(['deploy','--profile',str(profile.source_path),*reference],folder,'02-deploy',180)
                if code: raise RuntimeError('显式部署失败')
            code,_=self.invoke(['verify-deployment','--profile',str(profile.source_path),*reference],folder,'03-verify-deployment',120)
            if code: raise RuntimeError('部署核验失败')
            command=['run','--profile',str(profile.source_path),'--test-id',self.test_id,'--attempt-id',attempt,
                     '--pc-artifacts','full','--overall-timeout',str(overall),*reference]
            if self.board.get('telemetry',True): command+=['--telemetry']
            code,result=self.invoke(command,folder,'04-run',overall+90)
            row.update(monitor_exit_code=code,verdict=result.get('verdict'),run_dir=str(run_dir))
            collect_code,collected=self.invoke(['collect','--test-id',self.test_id,'--attempt-id',attempt,'--artifact-set','full','--verify-hashes'],folder,'05-collect',180)
            row['collection_verified']=collect_code==0 and collected.get('verified') is True
            if not self.args.dry_run:
                result_path=run_dir/'result.json'
                if result_path.is_file():
                    local=load(result_path);row['verdict']=local.get('verdict')
                    self.invoke(['report','--run-dir',str(run_dir),'--format','markdown,json,csv'],folder,'06-report',60)
                native=self.output/self.test_id/'device-evidence'/attempt/'spool/workload-summary.json'
                summary=load(native) if native.is_file() else {}
                row.update(measurements(summary,profile,config,row.get('verdict'),row['collection_verified']))
                if not summary or summary.get('result')!='PASS' or summary.get('exit_code')!=0 or row['verification_issues']:
                    raise RuntimeError('设备 native summary 缺失、不通过或校验证据无效')
            row['state']='COMPLETED' if code==0 and row['collection_verified'] else 'FAILED'
        except KeyboardInterrupt:
            row.update(state='INTERRUPTED',error='操作者中止，需核对设备进程与完整收集');raise
        except (OSError,ValueError,RuntimeError) as exc:
            row.update(state='FAILED',error=str(exc))
        finally:
            row['finished_at']=now()
            if not self.args.dry_run: append_ledger(self.ledger,row)
        return row['state']=='COMPLETED' or self.args.dry_run

    def run(self):
        day=plan_stages(self.plan)[self.args.day-1]
        lock=self.campaign/'.runner.lock'
        if self.args.dry_run:
            for cycle in range(1,day.get('cycles',1)+1):
                for name in day['profiles']: self.execute(name,cycle)
            return 0
        self.campaign.mkdir(parents=True,exist_ok=True)
        try: descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError: raise ValueError('campaign 已有 runner 锁；确认原进程停止后再人工清理，禁止并行抢用同一串口')
        os.write(descriptor,f'pid={os.getpid()}\nstarted_at={now()}\n'.encode());os.close(descriptor)
        try:
            snapshot=self.campaign/'configuration'/f'{self.board["board_id"]}-{self.identity[:12]}'
            snapshot.mkdir(parents=True,exist_ok=True)
            atomic_write_json(snapshot/'plan.json',self.plan);atomic_write_json(snapshot/'board.json',self.board)
            probe_dir=self.campaign/'operations'/self.board['board_id']/f'probe-D{self.args.day}-{uuid.uuid4().hex[:6]}'
            probe_dir.mkdir(parents=True)
            code,_=self.invoke(['probe','--platform',self.board['platform']],probe_dir,'00-probe',90)
            if code:
                append_ledger(self.ledger,{'schema_version':1,'day':self.args.day,'board_id':self.board['board_id'],'profile':'平台探测','target':'platform','state':'FAILED','started_at':now(),'finished_at':now(),'error':'平台探测失败；停止执行','collection_verified':False,'operations_dir':str(probe_dir)})
                raise RuntimeError('板卡平台探测失败；停止执行以避免把其他 SoC 当作模板平台')
            for cycle in range(1,day.get('cycles',1)+1):
                for name in day['profiles']:
                    success=self.execute(name,cycle)
                    if not success and not self.args.continue_on_error: return 1
            return 0
        finally: lock.unlink(missing_ok=True)


def summarize(campaign):
    campaign=campaign.resolve();rows=ledger_rows(campaign/'ledger.jsonl')
    days=[]
    stage_ids={r.get('day') for r in rows if type(r.get('day')) is int}
    for snapshot in (campaign/'configuration').glob('*/plan.json'):
        saved=validate_plan(load(snapshot));stage_ids.update(range(1,len(plan_stages(saved))+1))
    for day in sorted(stage_ids or {1}):
        selected_rows=[r for r in rows if r.get('day')==day]
        passed=[r for r in selected_rows if r.get('state')=='COMPLETED' and r.get('collection_verified') is True]
        actual=[r['measured_workload_s'] for r in selected_rows if finite(r.get('measured_workload_s'))]
        accepted=[r['accepted_load_s'] for r in passed if finite(r.get('accepted_load_s'))]
        days.append({'day':day,'status':'未执行' if not selected_rows else '有失败/中断' if any(r.get('state') in ('FAILED','INTERRUPTED') for r in selected_rows) else '已记录',
                     'attempts':len([r for r in selected_rows if r.get('state')!='SKIPPED']),'passed':len(passed),
                     'failed':len([r for r in selected_rows if r.get('state') in ('FAILED','INTERRUPTED')]),
                     'skipped':len([r for r in selected_rows if r.get('state')=='SKIPPED']),
                     'measured_workload_s':sum(actual) if actual else None,'accepted_load_s':sum(accepted) if accepted else None})
    dates=sorted({datetime.fromisoformat(r['started_at']).astimezone(TZ).date().isoformat() for r in rows if isinstance(r.get('started_at'),str)})
    report={'recorded_calendar_days':len(dates),'actual_dates':dates,'schema_version':1,'generated_at':now(),'campaign':str(campaign),'record_count':len(rows),'days':days,
            'scope':'用例运行与稳定性观察；不证明降压/Vmin/功耗收益','ledger_sha256':sha256_file(campaign/'ledger.jsonl') if rows else None}
    lines=['# 压测用例运行记录汇总','',f'生成时间：{report["generated_at"]}。统计口径：实际 native summary 的测量时长，排除主机等待、预热和框架后端。',
           '',report['scope'],'',f'实际记录覆盖 {len(dates)} 个日历日期：{", ".join(dates) if dates else "尚无"}。','','尚无板端记录；以下为未执行状态。' if not rows else f'共 {len(rows)} 条原始台账记录，失败与重试均保留。','',
           '| 阶段 | 状态 | 尝试 | 通过且证据完整 | 失败或中断 | 跳过 | 有测量依据的负载秒数 | 通过且证据完整的负载秒数 |','|---|---|---:|---:|---:|---:|---:|---:|']
    for day in days:
        values=[day[k] for k in ('day','status','attempts','passed','failed','skipped','measured_workload_s','accepted_load_s')]
        lines.append('| '+' | '.join('缺少有效时长' if v is None else f'{v:.3f}' if type(v) is float else str(v) for v in values)+' |')
    lines+=['','## 异常与跳过明细','']
    for row in rows:
        if row.get('state')!='COMPLETED': lines.append(f'- D{row.get("day")} {row.get("board_id")} {row.get("profile")}：{row.get("state")}；{row.get("error") or row.get("skip_reason") or row.get("verdict")}')
    lines+=['','阶段编号不代表日历日期或连续运行时长。未执行、跳过或中途失败应保留空缺与原因。原始 JSONL、stdout/stderr、设备 spool、配置和哈希必须随交接目录留存。','']
    destination=campaign/'summary';destination.mkdir(parents=True,exist_ok=True)
    atomic_write_json(destination/'stress-summary.json',report)
    (destination/'压测汇总.md').write_text('\n'.join(lines),encoding='utf-8')
    print(destination/'压测汇总.md');return report


def configure_parser(parser):
    sub=parser.add_subparsers(dest='stress_action',required=True)
    validate=sub.add_parser('validate-plan',help='检查压测计划');validate.add_argument('--plan',type=Path,required=True)
    run=sub.add_parser('run',help='执行一个压测阶段');run.add_argument('--plan',type=Path,required=True);run.add_argument('--board',type=Path,required=True);run.add_argument('--campaign',type=Path,required=True)
    run.add_argument('--stage',type=int,required=True);run.add_argument('--prepare',action='store_true');run.add_argument('--dry-run',action='store_true');run.add_argument('--retry-failed',action='store_true');run.add_argument('--continue-on-error',action='store_true')
    summary=sub.add_parser('summarize',help='按真实记录汇总');summary.add_argument('--campaign',type=Path,required=True)


def command(args,paths):
    try:
        if args.stress_action=='validate-plan':
            validate_plan(load(args.plan));print('压测计划结构有效；这不代表板卡或部署已验证');return 0
        if args.stress_action=='summarize':summarize(args.campaign);return 0
        args.day=args.stage;args.runtime_paths=paths
        return Runner(args).run()
    except KeyboardInterrupt:print('已中止；请检查设备进程并保留现场',file=sys.stderr);return 130
    except (OSError,ValueError,RuntimeError) as exc:print(str(exc),file=sys.stderr);return 2


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    validate=sub.add_parser('validate-plan');validate.add_argument('--plan',type=Path,required=True)
    run=sub.add_parser('run-day');run.add_argument('--plan',type=Path,required=True);run.add_argument('--board',type=Path,required=True);run.add_argument('--campaign',type=Path,required=True)
    run.add_argument('--day',type=int,choices=range(1,8),required=True);run.add_argument('--prepare',action='store_true',help='显式执行 deploy')
    run.add_argument('--dry-run',action='store_true');run.add_argument('--retry-failed',action='store_true');run.add_argument('--continue-on-error',action='store_true')
    summary=sub.add_parser('summarize');summary.add_argument('--campaign',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.action=='validate-plan': validate_plan(load(args.plan));print('压测计划结构有效；这不代表板卡或部署已验证');return 0
        if args.action=='summarize': summarize(args.campaign);return 0
        return Runner(args).run()
    except KeyboardInterrupt: print('已中止；请检查设备进程并保留现场',file=sys.stderr);return 130
    except (OSError,ValueError,RuntimeError) as exc: print(str(exc),file=sys.stderr);return 2


if __name__=='__main__': raise SystemExit(main())
