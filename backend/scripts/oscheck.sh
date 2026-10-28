#!/usr/bin/env bash
# 如果当前不是 bash，则自动切换到 bash 执行
if command -v bash >/dev/null 2>&1; then
	if [ -z "$BASH_VERSION" ]; then
		exec bash "$0" "$@"
	fi
fi
# 清理本地化环境变量，保证输出结果稳定一致
unset $(env | grep -E '^(LANG|LC_|LANGUAGE)' | cut -d= -f1) 2>/dev/null
export LC_ALL=C LANG=C
#==============================================================#
# 文件       :   OS Health Check
# 创建时间   :   2022-08-28 23:32:09
# 修改时间   :   2026-06-08 17:30:00
# 描述       :   Oracle Database OS Health Check script
# 版本       :   3.0.1-fixed
# 作者       :   Lucifer (ora100.com)
# 版权所有 (C) 2021-2100 Lucifer (ora100.com)
#==============================================================#
export PS4='+${BASH_SOURCE}:${LINENO}:${FUNCNAME[0]}: '
#==============================================================#
# 全局变量                    #
#==============================================================#
# 获取脚本所在目录，避免依赖 readlink -f
scripts_dir=$(cd -P "$(dirname "$0")" && pwd)
# 自定义参数默认值
SQL_SCRIPT=""
workdir=""
# 获取当前主机正在运行 Oracle 实例的软件用户，避免依赖 pgrep
dbusers=$(ps -eo user=,args= 2>/dev/null | awk '/([o]ra|[d]b)_pmon_/ {print $1}' | sort -u)
[[ -z "$dbusers" ]] && dbusers="$USER"
# 主机名后续会优先从数据库中获取
hostname=$(hostname | awk -F '.' '{print $1}')
date=$(date +%Y%m%d)
result_dir="$scripts_dir/dbcheck_${hostname}_${date}"
filename="${result_dir}/oscheck_${hostname}_${date}.txt"
tarname="${scripts_dir}/dbcheck_${hostname}_${date}.tar.gz"
# 自动检测 GI_HOME，避免误取 ohasd.bin 参数
_detect_gi_home() {
	local gh="" p
	# 1）优先从 /etc/oracle/olr.loc 获取
	if [[ -r /etc/oracle/olr.loc ]]; then
		gh=$(awk -F= '/^crs_home=/{print $2; exit}' /etc/oracle/olr.loc)
	fi
	# 2）兜底方式：从正在运行的 ohasd.bin 绝对路径推导
	if [[ -z "$gh" ]]; then
		p=$(ps -eo args= 2>/dev/null | awk '/[o]hasd\.bin/{print $1; exit}')
		[[ "$p" == /*/bin/ohasd.bin ]] && gh="${p%/bin/ohasd.bin}"
	fi
	# 3）校验结果：必须是绝对路径，且 olsnodes 可执行
	if [[ -n "$gh" && "$gh" == /* && -x "$gh/bin/olsnodes" ]]; then
		echo "$gh"
	fi
}
GI_HOME=$(_detect_gi_home)
# 检测操作系统类型
_OS=$(uname -s)
#==============================================================#
# 彩色输出函数                        #
#==============================================================#
function color_printf() {
	local res='\E[0m' default_color='\E[1;32m'
	case "$1" in
	"red")
		color='\E[1;31m'
		;;
	"green")
		color='\E[1;32m'
		;;
	"blue")
		color='\E[1;34m'
		;;
	"light_blue")
		color='\E[1;94m'
		;;
	"purple")
		color='\033[35m'
		;;
	*)
		color=${default_color}
		;;
	esac
	case "$1" in
	"red")
		printf "\n${color}%-20s %-30s %-50s\n${res}\n" "$2" "$3" "$4"
		exit 1
		;;
	"green" | "light_blue")
		printf "${color}%-20s %-30s %-50s\n${res}" "$2" "$3" "$4"
		;;
	"purple")
		printf "${color}%-s${res}" "$2" "$3"
		read -r con_flag
		if [[ -z $con_flag ]]; then
			con_flag=Y
		fi
		if [[ $con_flag != "Y" ]]; then
			echo
			exit 1
		fi
		;;
	*)
		printf "${color}%-20s %-30s %-50s\n${res}\n" "$2" "$3" "$4"
		;;
	esac
}
#==============================================================#
# 日志打印函数                           #
#==============================================================#
function log_print() {
	echo
	color_printf green "#==============================================================#"
	color_printf green "$1"
	color_printf green "#==============================================================#"
	echo
}
function check_file() {
	if [[ -e "$1" ]]; then
		return 0
	else
		return 1
	fi
}
function is_exact_db_user() {
	local u
	for u in $dbusers; do
		[[ "$u" == "$USER" ]] && return 0
	done
	return 1
}
function is_smon_running() {
	local sid="$1"
	[[ -z "$sid" ]] && return 1
	# 优先使用 pgrep 精确匹配完整进程名；没有 pgrep 时使用 ps+awk 兜底。
	if command -v pgrep >/dev/null 2>&1; then
		pgrep -x -f "ora_smon_${sid}" >/dev/null 2>&1 && return 0
		pgrep -x -f "db_smon_${sid}" >/dev/null 2>&1 && return 0
		pgrep -x -f "asm_smon_${sid}" >/dev/null 2>&1 && return 0
	fi
	ps -eo args= 2>/dev/null | awk -v t1="ora_smon_${sid}" -v t2="db_smon_${sid}" -v t3="asm_smon_${sid}" '
		{ for (i=1;i<=NF;i++) if ($i==t1 || $i==t2 || $i==t3) found=1 }
		END { exit found ? 0 : 1 }
	'
}
function get_cpu_usage_value() {
	# 通过 vmstat 表头定位 idle(id) 列，避免不同 OS/版本列号不一致。
	vmstat 1 2 2>/dev/null | awk '
		/id/ { for (i=1;i<=NF;i++) if ($i=="id") idle_col=i }
		END {
			if (idle_col > 0 && $idle_col ~ /^[0-9.]+$/) {
				printf "%.1f\n", 100 - $idle_col
			} else {
				print "N/A"
			}
		}
	'
}
function setup_sqlplus_isolation() {
	# 不再 mv $ORACLE_HOME/sqlplus/admin/glogin.sql，避免并发/中断导致原文件丢失。
	OSCHECK_TMPDIR="${TMPDIR:-/tmp}/oscheck.$$"
	mkdir -p "$OSCHECK_TMPDIR" 2>/dev/null || return 0
	: > "$OSCHECK_TMPDIR/glogin.sql" 2>/dev/null || true
	export SQLPATH="$OSCHECK_TMPDIR"
	unset ORACLE_PATH
}
#==============================================================#
# 使用帮助                            #
#==============================================================#
function help() {
	print_options() {
		local options=("$@")
		for option in "${options[@]}"; do
			color_printf green "${option%% *}" "${option#* }"
		done
	}
	echo
	color_printf blue "用法: oscheck.sh [选项]"
	color_printf blue "选项: "
	options=(
		"-o 指定要巡检的 Oracle 实例名。单实例: -o orcl  多实例: -o orcl,dbateamdb  全部实例: -o all  不指定时进入交互菜单，生产批量巡检推荐使用 -o all"
		"-d 工作目录，巡检结果输出到该目录。默认: 脚本所在目录。示例: -d /u01/oralog/autodbcheck/OracleHealthCheck"
		"-s 指定 SQL 巡检脚本文件名。示例: -s dbcheck_cdb.sql"
		"-h 显示本帮助信息"
	)
	print_options "${options[@]}"
	echo
	color_printf blue "示例: "
	color_printf green "  oscheck.sh                                  交互式选择实例进行巡检"
	color_printf green "  oscheck.sh -o orcl                          巡检指定实例 orcl"
	color_printf green "  oscheck.sh -o orcl,dbateamdb                同时巡检多个实例"
	color_printf green "  oscheck.sh -o all                           巡检本机所有运行中的实例"
	color_printf green "  oscheck.sh -o orcl -d /tmp/check            指定工作目录"
	color_printf green "  oscheck.sh -o orcl -s dbcheck_cdb.sql       指定 SQL 巡检脚本"
	echo
}
#==============================================================#
# 系统命令封装函数                     #
#==============================================================#
function oscmd() {
	local cmd="$1" h rhost
	if [[ "$israc" = "YES" ]]; then
		echo "** $hostname:"
		bash -c "$cmd" 2>/dev/null
		for h in "${remotehost[@]}"; do
			rhost=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$h" hostname 2>/dev/null)
			[[ -z "$rhost" ]] && rhost="$h"
			echo "** $rhost:"
			ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no "$h" "$cmd" 2>/dev/null
		done
	else
		echo "** $hostname:"
		bash -c "$cmd" 2>/dev/null
	fi
}
#==============================================================#
# 补丁信息                           #
#==============================================================#
function get_patch_info() {
	if [[ "$dbver" != "10.2" ]]; then
		echo "dbpatch"
		"$ORACLE_HOME"/OPatch/opatch lspatches 2>/dev/null | sed '$d;/^$/d'
		echo
		if [[ -n "$GI_HOME" ]]; then
			echo "gipatch"
			"$GI_HOME"/OPatch/opatch lspatches 2>/dev/null | sed '$d;/^$/d'
			echo
		fi
	fi
}
#==============================================================#
# 监听器信息                        #
#==============================================================#
function get_lsnr_info() {
	local lsnrctl_path=$ORACLE_HOME/bin/lsnrctl
	echo "lsnrctl"
	if [[ "$israc" == "YES" ]] && ! [[ "$dbver" == "10.2" || "$dbver" == "11.1" ]]; then
		lsnrctl_path=$GI_HOME/bin/lsnrctl
		local srvctl_path=$GI_HOME/bin/srvctl
		$srvctl_path status listener 2>/dev/null | grep -v "^\s*\(#\|$\)"
	fi
	$lsnrctl_path status listener 2>/dev/null | grep -v "^\s*\(#\|$\)"
	echo
	echo "sqlnet"
	if [ -f "$ORACLE_HOME/network/admin/sqlnet.ora" ]; then
		grep -v "^\s*\(#\|$\)" "$ORACLE_HOME/network/admin/sqlnet.ora"
	fi
	echo
}
#==============================================================#
# 操作系统信息收集                        #
#==============================================================#
function get_os_info() {
	local commands command
	commands=(
		"os_type"
		"rac"
		"osversion"
		"kernel"
		"cpu"
		"cpuusage"
		"memtotal"
		"memusage"
		"swap"
		"swapusage"
		"loadaverage"
		"upday"
		"time"
		"hosts"
		"sysctl"
		"limits"
		"diskusage"
		"inode"
		"meminfo"
		"freemem"
		"thp"
		"crontab"
	)
	if [[ "$israc" == "YES" ]]; then
		commands+=("ocrcheck" "votedisk" "ocrbackup" "oifcfg")
		commands+=("crsstat" "olsnode")
		[[ "$dbver" != "10.2" && "$dbver" != "11.1" ]] && commands+=("crsinit")
	fi
	for command in "${commands[@]}"; do
		echo "$command"
		case "$command" in
		"os_type") echo "$_OS" ;;
		"rac") echo "$israc" ;;
		"osversion")
			case "$_OS" in
			Linux) cat /etc/*release 2>/dev/null | head -n 1 ;;
			AIX) oslevel -s 2>/dev/null ;;
			HP-UX) uname -r ;;
			*) uname -s -r ;;
			esac
			;;
		"kernel") uname -r ;;
		"cpu")
			case "$_OS" in
			Linux) awk -F': ' '/model name/ {print $2; exit}' /proc/cpuinfo ;;
			AIX) prtconf 2>/dev/null | grep 'Processor Type' | head -n 1 ;;
			HP-UX) machinfo 2>/dev/null | grep -i processor | head -n 1 || getconf MACHINE_MODEL 2>/dev/null ;;
			*) uname -m ;;
			esac
			;;
		"cpuusage")
			get_cpu_usage_value
			;;
		"memtotal")
			case "$_OS" in
			Linux) free -m | awk '/Mem:/ {print $2/1024}' ;;
			AIX) svmon -G 2>/dev/null | awk '/^memory / {printf "%.1f", ($2*4)/1048576}' ;;
			HP-UX) swapinfo -m 2>/dev/null | awk '/^memory/ {printf "%.1f", $2/1024}' ;;
			*) echo "N/A" ;;
			esac
			;;
		"memusage")
			case "$_OS" in
			Linux) free -m | awk '/Mem:/ {if ($2>0) print $3/$2*100; else print "N/A"}' ;;
			AIX) svmon -G 2>/dev/null | awk '/^memory / {if ($2>0) printf "%.1f", ($3/$2)*100; else print "N/A"}' ;;
			HP-UX) swapinfo -m 2>/dev/null | awk '/^memory/ {if ($2>0) printf "%.1f", ($3/$2)*100; else print "N/A"}' ;;
			*) echo "N/A" ;;
			esac
			;;
		"swap")
			case "$_OS" in
			Linux) free -m | awk '/Swap:/ {print $2/1024}' ;;
			AIX) lsps -s 2>/dev/null | awk 'NR==2 {gsub(/MB/,"",$1); printf "%.1f", $1/1024}' ;;
			HP-UX) swapinfo -m 2>/dev/null | awk '/^dev/ {total+=$2} END {printf "%.1f", total/1024}' ;;
			*) echo "N/A" ;;
			esac
			;;
		"swapusage")
			case "$_OS" in
			Linux) free -m | awk '/Swap:/ {if ($2>0) print $3/$2*100; else print 0}' ;;
			AIX) lsps -s 2>/dev/null | awk 'NR==2 {gsub(/%/,"",$2); print $2}' ;;
			HP-UX) swapinfo -m 2>/dev/null | awk '/^dev/ {used+=$3; total+=$2} END {if (total>0) printf "%.1f", (used/total)*100; else print 0}' ;;
			*) echo "N/A" ;;
			esac
			;;
		"loadaverage") w | grep "load average" | awk -F ": " '{print $2}' ;;
		"upday") w | head -n 1 | awk -F ", " '{print $1}' | cut -c 11- ;;
		"time") date +"%Y-%m-%d %H:%M:%S" ;;
		"hosts") sed '1,2d' /etc/hosts | grep -v '^$' ;;
		"sysctl")
			case "$_OS" in
			Linux) grep -E "kernel.shmall|kernel.shmmax|kernel.sem|kernel.shmmni|fs.aio-max-nr|fs.file-max|net.ipv4.ip_local_port_range|net.core.rmem_default|net.core.rmem_max|net.core.wmem_default|net.core.wmem_max|vm.swappiness|vm.nr_hugepages|vm.min_free_kbytes" /etc/sysctl.conf ;;
			AIX)
				echo "--- no -a ---"
				no -a 2>/dev/null
				echo "--- vmo -a ---"
				vmo -a 2>/dev/null
				echo "--- ioo -a ---"
				ioo -a 2>/dev/null
				;;
			HP-UX) kctune 2>/dev/null ;;
			*) echo "N/A" ;;
			esac
			;;
		"limits")
			case "$_OS" in
			Linux) grep -v "^\s*\(#\|$\)" /etc/security/limits.conf ;;
			AIX)
				for _u in $dbusers; do
					echo "--- $_u ---"
					lsuser -a fsize data stack rss nofiles "$_u" 2>/dev/null
				done
				;;
			HP-UX) kctune 2>/dev/null | grep -i -E "maxfiles|maxproc|shmmax|shmmni|semmns|semmsl|semmni|nproc" ;;
			*) echo "N/A" ;;
			esac
			;;
		"diskusage")
			case "$_OS" in
			Linux) oscmd "df -PTh" ;;
			*) oscmd "df -Pk" ;;
			esac
			;;
		"inode")
			case "$_OS" in
			Linux) oscmd "df -PTi" ;;
			AIX) oscmd "df -Pi" ;;
			*) oscmd "df -Pi" ;;
			esac
			;;
		"meminfo")
			case "$_OS" in
			Linux) awk -F": " '/MemTotal|MemFree|MemAvailable|Cached|SwapTotal|SwapFree|AnonHugePages|HugePages_Total|HugePages_Free/ {print $1":"$2}' /proc/meminfo ;;
			AIX) svmon -G 2>/dev/null | awk '
                    /^memory / {printf "MemTotal: %d kB\nMemFree: %d kB\nMemUsed: %d kB\n", $2*4, ($2-$3)*4, $3*4}
                    /^pg space/ {printf "SwapTotal: %d kB\nSwapFree: %d kB\n", $3*4, ($3-$4)*4}
                    ' ;;
			HP-UX) swapinfo -m 2>/dev/null | awk '
                    /^memory/ {printf "MemTotal: %.0f kB\nMemUsed: %.0f kB\nMemFree: %.0f kB\n", $2*1024, $3*1024, $4*1024}
                    /^dev/ {printf "SwapTotal: %.0f kB\nSwapUsed: %.0f kB\nSwapFree: %.0f kB\n", $2*1024, $3*1024, $4*1024}
                    ' ;;
			*) echo "N/A" ;;
			esac
			;;
		"freemem")
			case "$_OS" in
			Linux) free -k ;;
			AIX) svmon -G 2>/dev/null | awk '
                    BEGIN {printf "%-15s %12s %12s %12s\n", "", "total", "used", "free"}
                    /^memory / {printf "%-15s %12d %12d %12d\n", "Mem:", $2*4, $3*4, ($2-$3)*4}
                    /^pg space/ {printf "%-15s %12d %12d %12d\n", "Swap:", $3*4, $4*4, ($3-$4)*4}
                    ' ;;
			HP-UX) swapinfo -m 2>/dev/null ;;
			*) echo "N/A" ;;
			esac
			;;
		"thp")
			case "$_OS" in
			Linux) [[ -e /sys/kernel/mm/transparent_hugepage/enabled ]] && cat /sys/kernel/mm/transparent_hugepage/enabled ;;
			*) echo "[always] madvise never" ;;
			esac
			;;
		"crontab") crontab -l 2>/dev/null ;;
		"ocrcheck") "$GI_HOME"/bin/ocrcheck | grep -v '^$' ;;
		"votedisk") "$GI_HOME"/bin/crsctl query css votedisk ;;
		"ocrbackup") "$GI_HOME"/bin/ocrconfig -showbackup | grep -v '^$' ;;
		"oifcfg") "$GI_HOME"/bin/oifcfg getif -global ;;
		"crsstat")
			if [[ "$dbver" == "10.2" || "$dbver" == "11.1" ]]; then
				"$GI_HOME/bin/crs_stat" -t
			else
				"$GI_HOME/bin/crsctl" stat res -t
			fi
			;;
		"olsnode")
			if [[ "$dbver" != "10.2" && "$dbver" != "11.1" ]]; then
				"$GI_HOME/bin/olsnodes" -n -i -s
			else
				"$GI_HOME/bin/olsnodes" -n -p -i
			fi
			;;
		"crsinit") "$GI_HOME"/bin/crsctl stat res -t -init ;;
		*) echo "未知巡检项: $command" ;;
		esac
		echo
	done
}
#==============================================================#
# 数据库巡检                           #
#==============================================================#
function get_db_report() {
	local SID
	IFS=',' read -r -a _oscheck_sids <<<"$ORACLE_SIDS"
	for SID in "${_oscheck_sids[@]}"; do
		[[ -z "$SID" ]] && continue
		export ORACLE_SID="$SID"
		log_print "正在检查数据库实例：$SID"
		# 切换到脚本目录，确保 html 生成在正确位置
		cd "$scripts_dir" || return
		if [[ -z "$SQL_SCRIPT" ]]; then
			color_printf red "数据库巡检脚本未设置，请检查 -s 参数或数据库版本识别结果!"
			return 1
		fi
		log_print "使用数据库巡检脚本：$SQL_SCRIPT"
		sqlplus -S / as sysdba @"$SQL_SCRIPT"
	done
}
#==============================================================#
# 压缩巡检日志                         #
#==============================================================#
function tar_logfile() {
	cd "$result_dir" || return 1
	# 安全移动数据库 HTML 报告：无匹配时不报错，文件名含空格也能处理。
	shopt -s nullglob
	local f
	for f in ../dbcheck_*html; do
		mv -- "$f" . 2>/dev/null || true
	done
	shopt -u nullglob
	case "$_OS" in
	Linux)
		# 不使用 -P，避免归档中保留绝对路径。
		if tar -czf "$tarname" .; then
			echo
			color_printf blue "压缩包生成位置: $tarname"
		else
			color_printf red "创建压缩包失败！"
			return 1
		fi
		;;
	*)
		if tar -cf - . | gzip >"$tarname"; then
			echo
			color_printf blue "压缩包生成位置: $tarname"
		else
			color_printf red "创建压缩包失败！"
			return 1
		fi
		;;
	esac
}
#==============================================================#
# Logo 打印                          #
#==============================================================#
function logo_print() {
	cat <<-EOF

		   ███████                             ██         ██      ██                    ██   ██   ██        ██████  ██                      ██
		  ██░░░░░██                           ░██        ░██     ░██                   ░██  ░██  ░██       ██░░░░██░██                     ░██
		 ██     ░░██ ██████  ██████    █████  ░██  █████ ░██     ░██  █████   ██████   ░██ ██████░██      ██    ░░ ░██       █████   █████ ░██  ██
		░██      ░██░░██░░█ ░░░░░░██  ██░░░██ ░██ ██░░░██░██████████ ██░░░██ ░░░░░░██  ░██░░░██░ ░██████ ░██       ░██████  ██░░░██ ██░░░██░██ ██
		░██      ░██ ░██ ░   ███████ ░██  ░░  ░██░███████░██░░░░░░██░███████  ███████  ░██  ░██  ░██░░░██░██       ░██░░░██░███████░██  ░░ ░████
		░░██     ██  ░██    ██░░░░██ ░██   ██ ░██░██░░░░ ░██     ░██░██░░░░  ██░░░░██  ░██  ░██  ░██  ░██░░██    ██░██  ░██░██░░░░ ░██   ██░██░██
		 ░░███████  ░███   ░░████████░░█████  ███░░██████░██     ░██░░██████░░████████ ███  ░░██ ░██  ░██ ░░██████ ░██  ░██░░██████░░█████ ░██░░██
		  ░░░░░░░   ░░░     ░░░░░░░░  ░░░░░  ░░░  ░░░░░░ ░░      ░░  ░░░░░░  ░░░░░░░░ ░░░    ░░  ░░   ░░   ░░░░░░  ░░   ░░  ░░░░░░  ░░░░░  ░░  ░░

		=========================================================================
		 Oracle 数据库主机健康巡检工具 v3.0.2
		 DBA 学习平台：https://ora100.com
		 作者微信：Lucifer-0622
		=========================================================================
	EOF
}
function checkpara_NULL() {
	if [[ -z $2 || $2 == -* ]]; then
		color_printf red "参数 [ $1 ] 的值为空，请检查！"
	fi
}
#==============================================================#
# 参数接收解析                      #
#==============================================================#
function accept_para() {
	while [[ $1 ]]; do
		case $1 in
		-o)
			checkpara_NULL "$1" "$2"
			ORACLE_SIDS=$2
			shift 2
			;;
		-d | --workdir)
			if [[ $2 ]]; then
				workdir=$2
			fi
			shift 2
			;;
		-s | --sql-script)
			if [[ $2 ]]; then
				SQL_SCRIPT=$2
			fi
			shift 2
			;;
		-h | --help)
			help
			exit 1
			;;
		*)
			echo
			color_printf red "参数错误: [$1]. 执行 './oscheck.sh -h' 获取帮助!"
			echo
			exit 1
			;;
		esac
	done
}
#==============================================================#
# 检测正在运行的 Oracle 实例              #
#==============================================================#
# 扫描本机 ora_pmon_* 进程，排除 ASM 实例（+ASM*）。
# 结果保存到全局数组 RUNNING_SIDS。
function discover_running_sids() {
	RUNNING_SIDS=()
	local line sid
	while IFS= read -r line; do
		# extract suffix after "ora_pmon_" or the Oracle Free/26ai "db_pmon_" prefix
		sid=${line##*ora_pmon_}
		sid=${sid##*db_pmon_}
		[[ -z "$sid" || "$sid" == +ASM* ]] && continue
		RUNNING_SIDS+=("$sid")
	done < <(ps -eo args= 2>/dev/null |
		awk '/([o]ra|[d]b)_pmon_/{for(i=1;i<=NF;i++) if($i ~ /^(ora|db)_pmon_/) print $i}' |
		sort -u)
}
#==============================================================#
# 交互式实例选择器                #
#==============================================================#
# 根据用户选择生成逗号分隔的 ORACLE_SIDS。
# - 如果指定了 -o 参数，则直接使用；如果是 all，则展开为所有实例。
# - 如果没有指定 -o 且当前为交互终端，则显示编号菜单。
# - 如果是非交互模式，则回退使用环境变量 ORACLE_SID。
function select_instances() {
	discover_running_sids

	# -o all：展开为所有正在运行的实例
	if [[ "$ORACLE_SIDS" == "all" || "$ORACLE_SIDS" == "ALL" ]]; then
		if [[ ${#RUNNING_SIDS[@]} -eq 0 ]]; then
			color_printf red "本机未检测到任何运行中的 Oracle 实例 (ora_pmon_*)"
			exit 1
		fi
		ORACLE_SIDS=$(
			IFS=,
			echo "${RUNNING_SIDS[*]}"
		)
		color_printf blue "已选择所有实例: $ORACLE_SIDS"
		return
	fi

	# -o 显式给值：不再提示用户选择
	[[ -n "$ORACLE_SIDS" ]] && return

	# 无 -o 且非交互：沿用旧逻辑，由 pre_todo 校验 ORACLE_SID
	if [[ ! -t 0 ]]; then
		return
	fi

	# 无 -o 且为交互终端：弹出实例选择菜单
	if [[ ${#RUNNING_SIDS[@]} -eq 0 ]]; then
		color_printf red "本机未检测到任何运行中的 Oracle 实例 (ora_pmon_*)"
		exit 1
	fi

	local default_idx="" i=0
	echo
	color_printf blue "检测到本机运行中的 Oracle 实例："
	for sid in "${RUNNING_SIDS[@]}"; do
		i=$((i + 1))
		if [[ -n "$ORACLE_SID" && "$sid" == "$ORACLE_SID" ]]; then
			default_idx=$i
			color_printf green "  [$i] $sid  (当前实例)"
		else
			color_printf green "  [$i] $sid"
		fi
	done
	echo
	color_printf blue "请选择要巡检的实例 (单选: 2 | 多选: 1,3 | 全选: a | 退出: q)"

	local attempts=0 ans selection=()
	while ((attempts < 3)); do
		attempts=$((attempts + 1))
		if [[ -n "$default_idx" ]]; then
			read -r -p "选择 [回车=${RUNNING_SIDS[default_idx - 1]}]: " ans
		else
			read -r -p "选择: " ans
		fi
		ans=${ans// /,} # 空格分隔也允许
		ans=${ans%,}
		ans=${ans#,}

		# 空回车且存在默认实例
		if [[ -z "$ans" && -n "$default_idx" ]]; then
			ORACLE_SIDS=${RUNNING_SIDS[default_idx - 1]}
			return
		fi
		case "$ans" in
		q | Q)
			color_printf blue "用户取消，退出。"
			exit 0
			;;
		a | A | all | ALL)
			ORACLE_SIDS=$(
				IFS=,
				echo "${RUNNING_SIDS[*]}"
			)
			color_printf blue "已选择所有实例: $ORACLE_SIDS"
			return
			;;
		esac

		# 解析用户输入的实例编号列表
		selection=()
		local ok=1 n
		IFS=',' read -r -a _picks <<<"$ans"
		for n in "${_picks[@]}"; do
			if [[ "$n" =~ ^[0-9]+$ ]] && ((n >= 1 && n <= ${#RUNNING_SIDS[@]})); then
				selection+=("${RUNNING_SIDS[n - 1]}")
			else
				ok=0
				break
			fi
		done
		if ((ok == 1 && ${#selection[@]} > 0)); then
			# 去重并保持原有顺序
			local -A seen=()
			local out=()
			for n in "${selection[@]}"; do
				[[ -z "${seen[$n]:-}" ]] && {
					out+=("$n")
					seen[$n]=1
				}
			done
			ORACLE_SIDS=$(
				IFS=,
				echo "${out[*]}"
			)
			color_printf blue "已选择实例: $ORACLE_SIDS"
			return
		fi
		color_printf red "输入无效，请重试。"
	done

	color_printf red "重试次数已达上限，退出。"
	exit 1
}
#==============================================================#
# 自定义参数解析                   #
#==============================================================#
function parse_custom_params() {
	# 1. 解析工作目录
	if [[ -n "$workdir" ]]; then
		if [[ -d "$workdir" ]]; then
			cd "$workdir" || exit 1
			scripts_dir=$(pwd)
		else
			color_printf red "工作目录不存在: $workdir"
			exit 1
		fi
	else
		# 默认使用脚本所在目录
		scripts_dir=$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)
	fi

	# 2. 从数据库获取主机名（统一短主机名）
	get_hostname_from_db

	# 3. 设置结果目录和输出文件
	result_dir="$scripts_dir/dbcheck_${hostname}_${date}"
	filename="${result_dir}/oscheck_${hostname}_${date}.txt"
	tarname="${scripts_dir}/dbcheck_${hostname}_${date}.tar.gz"

	# 4. 解析 SQL 脚本
	parse_sql_script "$SQL_SCRIPT"
}

#==============================================================#
# 从数据库获取主机名                      #
#==============================================================#
function get_hostname_from_db() {
	# 从 v$instance 获取当前实例的主机名（统一短主机名）
	hostname=$(
		sqlplus -S / as sysdba <<EOF
    set heading off feedback off pagesize 0
    SELECT REGEXP_SUBSTR(host_name, '[^.]+', 1, 1) FROM v\$instance;
    exit;
EOF
	)
	hostname=$(printf '%s' "$hostname" | tr -s '[:space:]' ' ' | sed -e 's/^ //' -e 's/ $//') # 清理空格

	if [[ -z "$hostname" ]]; then
		# 如果查询失败，回退到 hostname 命令
		hostname=$(hostname | awk -F '.' '{print $1}')
	fi
}

#==============================================================#
# 从数据库获取实例列表                     #
#==============================================================#
function get_instances_from_db() {
	# 优先使用 olsnodes（返回短主机名）
	if [[ -n "$GI_HOME" && -x "$GI_HOME/bin/olsnodes" ]]; then
		local olnodes_result=$("$GI_HOME"/bin/olsnodes 2>/dev/null | awk '{print $1}' | cut -d'.' -f1)
		if [[ -n "$olnodes_result" ]]; then
			printf '%s' "$olnodes_result" | tr -s '[:space:]' ' ' | sed -e 's/^ //' -e 's/ $//'
			return 0
		fi
	fi

	# 回退到 gv$instance，使用 REGEXP_SUBSTR 提取短主机名
	local instances=$(
		sqlplus -S / as sysdba <<EOF
    set heading off feedback off pagesize 0
    SELECT DISTINCT REGEXP_SUBSTR(host_name, '[^.]+', 1, 1) FROM gv\$instance ORDER BY 1;
    exit;
EOF
	)

	# 清理 SQL 输出，统一为空格分隔，避免后续数组解析出现 btdbt01,btdbt02 这类错误
	instances=$(printf '%s' "$instances" | tr -s '[:space:]' ' ' | sed -e 's/^ //' -e 's/ $//')

	if [[ -n "$instances" ]]; then
		echo "$instances"
	else
		hostname | awk -F '.' '{print $1}'
	fi
}

#==============================================================#
# 解析 SQL 脚本                          #
#==============================================================#
function parse_sql_script() {
	local sql_script="$1"
	# 如果 dbver 未定义，则先获取数据库版本
	if [[ -z "$dbver" ]]; then
		dbver=$(sqlplus -v | awk '/SQL/{print substr($3,1,4)}')
	fi
	if [[ -n "$sql_script" ]]; then
		if [[ "$sql_script" == /* ]] && check_file "$sql_script"; then
			SQL_SCRIPT="$sql_script"
		elif check_file "$scripts_dir/$sql_script"; then
			SQL_SCRIPT="$sql_script"
		else
			color_printf red "SQL脚本不存在: $sql_script"
			exit 1
		fi
	else
		# 默认行为：根据数据库版本自动选择 SQL 巡检脚本
		case "$dbver" in
		"10.2") SQL_SCRIPT="dbcheck10g.sql" ;;
		"11.1" | "11.2") SQL_SCRIPT="dbcheck_noncdb.sql" ;;
		"12.1" | "12.2" | "18."* | "19."* | "21."* | "23."* | "26."*)
			# 12c 及以上版本需要检测是否为 CDB 架构
			is_cdb=$(
				sqlplus -S / as sysdba <<EOSQL
SET HEADING OFF FEEDBACK OFF PAGESIZE 0
SELECT CDB FROM V\$DATABASE;
EXIT;
EOSQL
			)
			is_cdb=$(echo "$is_cdb" | tr -d '[:space:]')
			if [ "$is_cdb" = "YES" ]; then
				SQL_SCRIPT="dbcheck_cdb.sql"
			else
				SQL_SCRIPT="dbcheck_noncdb.sql"
			fi
			;;
		*)
			color_printf red "不支持的数据库版本: $dbver"
			exit 1
			;;
		esac
	fi
}
function pre_todo() {
	# 检查当前用户是否为 Oracle 数据库软件用户，必须精确匹配，避免 ora 误匹配 oracle。
	if ! is_exact_db_user; then
		color_printf red "当前用户 $USER 不是 Oracle 数据库软件用户，请使用以下用户之一运行此脚本：$dbusers"
	fi

	# 解析实例列表：未指定 -o 时必须显式使用 ORACLE_SID，否则直接退出，避免空 SID 误命中任意 SMON。
	if [[ -z "$ORACLE_SIDS" ]]; then
		if [[ -n "$ORACLE_SID" ]]; then
			ORACLE_SIDS="$ORACLE_SID"
		else
			color_printf red "未指定 -o 参数，且环境变量 ORACLE_SID 为空，请使用 -o 指定实例或设置 ORACLE_SID"
		fi
	fi

	local SID
	IFS=',' read -r -a _pre_sids <<<"$ORACLE_SIDS"
	for SID in "${_pre_sids[@]}"; do
		[[ -z "$SID" ]] && continue
		if ! is_smon_running "$SID"; then
			color_printf red "巡检数据库 [ $SID ] 未启动，请先启动数据库实例或者检查 [ -o ] 参数值 $ORACLE_SIDS 是否正确!"
		fi
	done

	# sqlplus 隔离：通过临时 SQLPATH 空 glogin.sql 规避 glogin.sql 输出影响，不再修改 ORACLE_HOME 下的原文件。
	setup_sqlplus_isolation

	[[ -e $result_dir ]] && rm -rf "$result_dir"
	mkdir -p "$result_dir"
	dbver=$(sqlplus -v | awk '/SQL/{print substr($3,1,4)}')
	NLS_LANG=$(sqlplus -S / as sysdba <<<"set heading off echo off termout off feed off time off timing off"$'\n'"select 'AMERICAN_AMERICA.'||property_value from database_properties where property_name = 'NLS_CHARACTERSET';")
	export NLS_LANG
	# RAC 架构检测及互信检查
	if [[ -n "$GI_HOME" && -x "$GI_HOME/bin/olsnodes" ]] && ps -eo args= 2>/dev/null | grep -q '[c]rsd\.bin'; then
		israc=YES
		echo "检测到 RAC 架构，开始检查节点 SSH 互信..."
		localhost=$("$GI_HOME"/bin/olsnodes -l | awk '{print $1}')
		allhost=$("$GI_HOME"/bin/olsnodes -n | awk '{print $1}')
		remotehost=()
		for node in $allhost; do
			[[ -n "$node" && "$node" != "$localhost" ]] && remotehost+=("$node")
		done
		failed_nodes=()
		for node in "${remotehost[@]}"; do
			echo -n "检查 SSH 互信: $localhost -> $node ... "
			if ssh -o BatchMode=yes -o ConnectTimeout=5 -o ConnectionAttempts=1 \
				-o StrictHostKeyChecking=no "$node" true >/dev/null 2>&1; then
				echo "OK"
			else
				echo "FAILED"
				failed_nodes+=("$node")
			fi
		done

		if [[ ${#failed_nodes[@]} -gt 0 ]]; then
			echo
			color_printf blue "警告: RAC 节点 SSH 互信失败，跳过远程节点采集: ${failed_nodes[*]}"
			color_printf blue "本次将继续采集本地节点，远程节点 df/资源信息可能为空。"
		else
			echo "所有节点 SSH 互信正常"
		fi
	else
		israc=NO
		allhost=$(get_instances_from_db)
	fi
}
function end_todo() {
	# 仅清理本次运行创建的临时 SQLPATH，绝不修改 ORACLE_HOME/sqlplus/admin/glogin.sql。
	if [[ -n "${OSCHECK_TMPDIR:-}" && "$OSCHECK_TMPDIR" == /tmp/oscheck.* ]]; then
		rm -rf "$OSCHECK_TMPDIR" 2>/dev/null || true
	fi
}

function print_promo() {
	echo "================================================================="
	echo "巡检工具来源：ora100.com"
	echo
	echo "提供："
	echo "√ Oracle / MySQL / PostgreSQL / SQL Server 等实战课程"
	echo "√ 数据库巡检报告自动生成"
	echo "√ 数据库一键安装脚本"
	echo "√ 数据库迁移实战课程"
	echo "√ DBA AI 助手"
	echo
	echo "更多 DBA 实战内容请访问："
	echo "https://ora100.com"
	echo "================================================================="
	echo
}

#==============================================================#
# 主函数入口                              #
#==============================================================#
function main() {
	set -o pipefail
	trap end_todo EXIT INT TERM HUP
	logo_print
	accept_para "$@"
	select_instances
	setup_sqlplus_isolation
	parse_custom_params
	pre_todo
	log_print "Oracle数据库主机检查"
	color_printf blue "正在收集主机操作系统信息..."
	get_os_info >>"$filename"
	color_printf blue "正在收集数据库补丁信息..."
	get_patch_info >>"$filename"
	color_printf blue "正在收集数据库监听信息..."
	get_lsnr_info >>"$filename"
	get_db_report
	tar_logfile
	color_printf blue "巡检报告文件路径: $filename"
	color_printf blue "巡检压缩包路径: $tarname"
	print_promo
	end_todo
}
main "$@"
