#!/bin/bash
if [[ $KILL_EPMD -eq 1 ]]; then
  epmd -kill || true
fi
epmd -daemon >/dev/null 2>&1 || true

set -e

# Generate host config if not exists
if [ ! -f "/usr/local/freeswitch/conf/freeswitch.xml" ]; then
  echo "Creating initial config..."
  mkdir -p /usr/local/freeswitch/conf
  cp -fra /defaults/conf/. /usr/local/freeswitch/conf/.
fi

# Fixing permissions
for d in $(echo "cache certs conf db fonts grammar htdocs images log mod recordings run scripts sounds storage"); do
  if [[ ! -d "/usr/local/freeswitch/$d" ]]; then
    mkdir -m 0777 -p "/usr/local/freeswitch/$d"
  fi
done
rm -fr /usr/local/freeswitch/log/freeswitch.log
tar --overwrite -xzf /defaults/sounds.tar.gz -C /usr/local/freeswitch/
chown -R 1000:1000 /usr/local/freeswitch

if [ "$1" = 'freeswitch' ]; then
  shift

  while :; do
    case $1 in
    -g | --g711-only)
      sed -i -e "s/global_codec_prefs=.*\"/global_codec_prefs=PCMU,PCMA\"/g" /usr/local/freeswitch/conf/vars.xml
      sed -i -e "s/outbound_codec_prefs=.*\"/outbound_codec_prefs=PCMU,PCMA\"/g" /usr/local/freeswitch/conf/vars.xml
      shift
      ;;

    -s | --sip-port)
      if [ -n "$2" ]; then
        sed -i -e "s/sip_port=[[:digit:]]\+/sip_port=$2/g" /usr/local/freeswitch/conf/vars_diff.xml
      fi
      shift
      shift
      ;;

    -t | --tls-port)
      if [ -n "$2" ]; then
        sed -i -e "s/tls_port=[[:digit:]]\+/tls_port=$2/g" /usr/local/freeswitch/conf/vars_diff.xml
      fi
      shift
      shift
      ;;

    -e | --event-socket-port)
      if [ -n "$2" ]; then
        sed -i -e "s/name=\"listen-port\" value=\"8021\"/name=\"listen-port\" value=\"$2\"/g" /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
      fi
      shift
      shift
      ;;

    -a | --rtp-range-start)
      if [ -n "$2" ]; then
        sed -i -e "s/name=\"rtp-start-port\" value=\".*\"/name=\"rtp-start-port\" value=\"$2\"/g" /usr/local/freeswitch/conf/autoload_configs/switch.conf.xml
      fi
      shift
      shift
      ;;

    -z | --rtp-range-end)
      if [ -n "$2" ]; then
        sed -i -e "s/name=\"rtp-end-port\" value=\".*\"/name=\"rtp-end-port\" value=\"$2\"/g" /usr/local/freeswitch/conf/autoload_configs/switch.conf.xml
      fi
      shift
      shift
      ;;

    --ext-rtp-ip)
      if [ -n "$2" ]; then
        sed -i -e "s/ext_rtp_ip=.*\"/ext_rtp_ip=$2\"/g" /usr/local/freeswitch/conf/vars_diff.xml
      fi
      shift
      shift
      ;;

    --ext-sip-ip)
      if [ -n "$2" ]; then
        sed -i -e "s/ext_sip_ip=.*\"/ext_sip_ip=$2\"/g" /usr/local/freeswitch/conf/vars_diff.xml
      fi
      shift
      shift
      ;;

    -p | --password)
      if [ -n "$2" ]; then
        sed -i -e "s/\(name=\"password\" value=\"\)[^\"]\+/\1$2/g" /usr/local/freeswitch/conf/autoload_configs/event_socket.conf.xml
      fi
      shift
      shift
      ;;

    -c | --cookie)
      if [ -n "$2" ]; then
        sed -e "s/\(name=\"cookie\" value=\"\)[^\"]\+/\1$2/g" /usr/local/freeswitch/conf/autoload_configs/erlang_event.conf.xml
      fi
      shift
      shift
      ;;

    --codec-answer-generous)
      sed -i -e "s/inbound-codec-negotiation\" value=\"greedy/inbound-codec-negotiation\" value=\"generous"/g /usr/local/freeswitch/conf/sip_profiles/mrf.xml
      shift
      ;;

    --codec-list)
      if [ -n "$2" ]; then
        sed -i -e "s/global_codec_prefs=.*\"/global_codec_prefs=$2\"/g" /usr/local/freeswitch/conf/vars.xml
        sed -i -e "s/outbound_codec_prefs=.*\"/outbound_codec_prefs=$2\"/g" /usr/local/freeswitch/conf/vars.xml
      fi
      shift
      shift
      ;;

    --username)
      if [ -n "$2" ]; then
        sed -e "s/\(name=\"username\" value=\"\)[^\"]\+/\1$2/g" /usr/local/freeswitch/conf/sip_profiles/mrf.xml
      fi
      shift
      shift
      ;;

    --advertise-external-ip)
      sed -i -e "s/ext-sip-ip\" value=\".*\"/ext-sip-ip\" value=\"\$\${ext_sip_ip}\""/g /usr/local/freeswitch/conf/sip_profiles/mrf.xml
      sed -i -e "s/ext-rtp-ip\" value=\".*\"/ext-rtp-ip\" value=\"\$\${ext_rtp_ip}\""/g /usr/local/freeswitch/conf/sip_profiles/mrf.xml
      shift
      ;;

    -l | --log-level)
      if [ -n "$2" ]; then
        sed -i -e "s/name=\"loglevel\" value=\".*\"/name=\"loglevel\" value=\"$2\"/g" /usr/local/freeswitch/conf/autoload_configs/switch.conf.xml
      fi
      shift
      shift
      ;;

    --)
      shift
      break
      ;;

    *)
      break
      ;;
    esac

  done

  exec freeswitch -u freeswitch -g freeswitch -nonat "$@"
  exit $?
fi

exec "$@"
