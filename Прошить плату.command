#!/bin/zsh
set -eu
cd -- "${0:A:h}"

if [[ "${1:-}" == '--help' || "${1:-}" == '-h' ]]; then
  print 'Использование: ./Прошить\ плату.command [USB-порт]'
  print 'Без аргумента выбирается единственный порт cu.usbserial-* или cu.wchusbserial*.'
  print 'Перед прошивкой остановите веб-сервис клавишами Ctrl+C в его окне Терминала.'
  exit 0
fi
if (( $# > 1 )); then
  print -u2 'Укажите не более одного USB-порта. Справка: ./Прошить\ плату.command --help'
  exit 1
fi

cli_path="$(command -v arduino-cli 2>/dev/null || true)"
if [[ -z "$cli_path" || ! -x "$cli_path" ]]; then
  cli_path="$HOME/.local/bin/arduino-cli"
fi
if [[ ! -x "$cli_path" ]]; then
  print -u2 'Не найден arduino-cli. Установите Arduino CLI в PATH или в ~/.local/bin.'
  exit 1
fi

print 'Перед прошивкой веб-сервис должен быть остановлен: нажмите Ctrl+C в его окне Терминала.'
print 'Этот скрипт не останавливает запущенный сервис автоматически.'
serial_port="${1:-}"
if [[ -z "$serial_port" ]]; then
  detected_ports=( /dev/cu.usbserial-*(N) /dev/cu.wchusbserial*(N) )
  if (( ${#detected_ports} == 0 )); then
    print -u2 'USB-плата не найдена. Подключите RF-Nano и запустите скрипт снова.'
    exit 1
  fi
  if (( ${#detected_ports} > 1 )); then
    print -u2 'Найдено несколько USB-портов. Укажите нужный порт аргументом:'
    print -rl -u2 -- "${detected_ports[@]}"
    exit 1
  fi
  serial_port="${detected_ports[1]}"
fi
if [[ ! -c "$serial_port" ]]; then
  print -u2 -- "USB-порт недоступен: $serial_port"
  exit 1
fi

build_dir="$(mktemp -d "${TMPDIR:-/private/tmp}/tractor-remote-build.XXXXXX")"
print -- "RF-Nano: $serial_port"
print -- "Каталог сборки: $build_dir"
"$cli_path" compile --fqbn arduino:avr:nano:cpu=atmega328 --build-path "$build_dir" rf_nano_controller
"$cli_path" upload --fqbn arduino:avr:nano:cpu=atmega328 --port "$serial_port" --input-dir "$build_dir" rf_nano_controller
print 'Прошивка загружена. Передача выключена. Теперь запустите пульт.'
