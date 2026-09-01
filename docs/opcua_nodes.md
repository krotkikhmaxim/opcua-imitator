# Ноды OPC UA имитатора

## Подключение

- Endpoint (сервер): `opc.tcp://0.0.0.0:4840`
- Endpoint (локально): `opc.tcp://127.0.0.1:4840`
- Namespace index: `2`
- Пространство имён: `urn:opcua-imitator`
- Всего нод: **101**

Формат NodeId: `ns=2;s=<полный IEC-путь>`. Переменные для внешних клиентов read-only; значения можно менять через UI имитатора (имитация).

## Таблица нод

| id | NodeId | Название | Тип | Канал | Шкаф | Модуль | Напр. | Бит/Слово | Устройство |
|---|---|---|---|---|---|---|---|---|---|

| pu-s1-avstop | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S1_AvStopPm` | Аварийный стоп ПМ | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 13 | S1 |
| pu-s2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S2_RezimRevizia` | Выбор режима «Ревизия» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 6 | S2 |
| pu-s21-reset-tp | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S21_ResetTp` | Зарядить предохранительный тормоз | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 29 | S21 |
| pu-s22-ack | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S22_Acknowledge` | Квитирование ошибок | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 30 | S22 |
| pu-s3 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S3_RezimLudi` | Выбор режима «Люди» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 7 | S3 |
| pu-s4 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S4_RezimGruz` | Выбор режима «Груз» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 8 | S4 |
| pu-s5 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S5_RezimNgbrt` | Выбор режима «Негабарит» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 9 | S5 |
| pu-s6 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S6_RezimSpuskMat` | Выбор режима «Спуск материалов» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 10 | S6 |
| pu-s7 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S7_BarabanOut` | Расцепить барабан | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 11 | S7 |
| pu-s8 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S8_BarabanIn` | Сцепить барабан | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 12 | S8 |
| pu-s9-fltpch | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.S9_FltPch` | Аварийное отключение ПЧ | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 15 | S9 |
| pu-sa10-comp1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA10_VyborKompres1` | Выбор компрессора 1 | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 3 | SA10 |
| pu-sa10-comp2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA10_VyborKompres2` | Выбор компрессора 2 | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 5 | SA10 |
| pu-sa10-off | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA10_OffKompres` | Отключение компрессоров | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 4 | SA10 |
| pu-sa25-auto | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA25_RezimAuto` | Автоматический режим тормоза | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 19 | SA25 |
| pu-sa25-manual | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA25_RezimManual` | Ручной режим тормоза | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 18 | SA25 |
| pu-sa26-test | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA26_RezimTest` | Режим «Тест» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 20 | SA26 |
| pu-sa27-bypass | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA27_RezimBypass` | Режим «Байпас» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 21 | SA27 |
| pu-sa28-off | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA28_PmOff` | Команда отключения ПМ | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 22 | SA28 |
| pu-sa28-on | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.SA28_PmOn` | Команда включения ПМ | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 23 | SA28 |
| pu-u1-down | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.U1_KarDown` | КАР: команда «Вниз» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 2 | U1 |
| pu-u1-up | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.U1_KarUp` | КАР: команда «Вверх» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 1 | U1 |
| pu-u1-zero | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.U1_KarZero` | КАР: рукоятка скорости в нуле | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 0 | U1 |
| pu-u2-braked | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.U2_KrtZatorm` | КРТ: положение «Заторможено» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 16 | U2 |
| pu-u2-released | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A2DigitalInput.U2_KrtRastorm` | КРТ: положение «Расторможено» | Boolean | Chanel1 | ПУ ПМ | 3A2 | DI | бит 17 | U2 |
| pu-s31-avstop | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.S31_AvStopPm` | Аварийный стоп ПМ | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 9 | S31 |
| pu-sa11-bypass | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.SA11_BypasShSS` | Обход защит ШСС | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 0 | SA11 |
| pu-sa12-perest | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.SA12_PezimPerest` | Режим перестановки | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 1 | SA12 |
| pu-sa13-left | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.SA13_SignalKlet1` | Источник команд: левая клеть | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 2 | SA13 |
| pu-sa13-mikon | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.SA13_SignalMikon` | Источник команд: ШАСС «МИКОН» | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 3 | SA13 |
| pu-sa13-right | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.SA13_Signalklet2` | Источник команд: правая клеть | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 4 | SA13 |
| pu-uz10 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x3A3DigitalInput.UZ10_FltPb` | Контроль питания 24 В ШВП | Boolean | Chanel1 | ПУ ПМ | 3A3 | DI | бит 16 | UZ10 |
| profibus-cu-fault-id | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.CUCurrentFaultID` | ID текущей ошибки управляющего устройства | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 18 | CU |
| profibus-current-filtered | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.CurrFilted` | Отфильтрованный ток | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 16 | Привод |
| profibus-inv-fault-id | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.InvCurrrentFaultID` | ID текущей ошибки инвертора | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 20 | Инвертор |
| profibus-inv-status-word | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.InvStatusWord` | Статусное слово инвертора | UInt16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 11 | Инвертор |
| profibus-inv-status-word-1 | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.InvStatusWord1` | Статусное слово инвертора 1 | UInt16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 12 | Инвертор |
| profibus-inv-status-word-2 | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.InvStatusWord2` | Статусное слово инвертора 2 | UInt16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 13 | Инвертор |
| profibus-motor-speed | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.MotorSpeed` | Скорость двигателя | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 15 | Привод |
| profibus-output-torque | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.OutputTorque` | Выходной момент | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 17 | Привод |
| profibus-rec-fault-id | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.RecCurrentFaultID` | ID текущей ошибки рекуператора | Int16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 19 | Рекуператор |
| profibus-rec-status-word | `ns=2;s=Chanel1.Application.GVL.DriveGVL.ProfibusData.RecStatusWord` | Статусное слово рекуператора | UInt16 | Chanel1 | Привод | PROFIBUS | PIW | сл. 14 | Рекуператор |
| shvp-k86 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.K86_FltVentOhl_1` | Обратная связь вентилятора двигателя 1 | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 21 | K86 |
| shvp-k87 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.K87_FltVentOhl_2` | Обратная связь вентилятора двигателя 2 | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 22 | K87 |
| shvp-qf1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF1_CtrlMcbVvod1` | Ввод 1 основного питания | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 0 | QF1 |
| shvp-qf10 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF10_FbMcbSobNuzBUT` | Собственные нужды БУТ | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 7 | QF10 |
| shvp-qf11 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF11_FbMcbRozPuPm` | Розетки ПУ ПМ | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 8 | QF11 |
| shvp-qf12 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF12_FbMcbSobNuzShvp` | Собственные нужды ШВП | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 9 | QF12 |
| shvp-qf14 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF14_CtrlMcbVvodIBP` | Ввод ИБП | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 10 | QF14 |
| shvp-qf16 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF16_CtrlMcbSobNuzShu` | Собственные нужды ШУ ПМ | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 11 | QF16 |
| shvp-qf17 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF17_CtrlMcbPch` | Собственные нужды ПЧВРЭ | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 12 | QF17 |
| shvp-qf2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF2_CtrlMcbVvod2` | Ввод 2 основного питания | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 1 | QF2 |
| shvp-qf24 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF24_FbMcbVolna` | Питание ШСС «Волна» от ИБП | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 25 | QF24 |
| shvp-qf28 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF28_FbMcbShu` | Питание ШУ от ИБП | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 27 | QF28 |
| shvp-qf29 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF29_FbMcbPchvre` | Питание ПЧВРЭ от ИБП | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 28 | QF29 |
| shvp-qf9 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.QF9_FbMcbSobNuzPPT` | Собственные нужды ППТ | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 6 | QF9 |
| shvp-u50 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.U50_FbBriz2` | Контроль БРИЗ 2 | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 31 | U50 |
| shvp-u6 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.U6_FbRkn` | Контроль напряжения, перекоса и фаз | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 20 | U6 |
| shvp-u7-nc | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.U7_FbRkiNz` | Контроль изоляции, NC | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 17 | U7 |
| shvp-u7-no | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.U7_FbRkiNo` | Контроль изоляции, NO | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 16 | U7 |
| shvp-uz1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A2DigitalInput.UZ1_FbMcbKntrPit` | Контроль источника питания 24 В | Boolean | Chanel1 | ШВП ПМ | 2A2 | DI | бит 14 | UZ1 |
| shvp-k1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K1_Vvod1Vkl` | Ввод 1 включён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 16 | K1 |
| shvp-k2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K2_Vvod1Otkl` | Ввод 1 отключён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 17 | K2 |
| shvp-k3 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K3_Vvod2Vkl` | Ввод 2 включён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 18 | K3 |
| shvp-k4 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K4_Vvod2Otkl` | Ввод 2 отключён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 19 | K4 |
| shvp-k6 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K6_Kompressor1Vkl` | Компрессор 1 включён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 21 | K6 |
| shvp-k7 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.K7_Kompressor2Vkl` | Компрессор 2 включён | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 22 | K7 |
| shvp-qf32 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.QF32_FbMcbIpbPu` | Питание ПУ от ИБП | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 1 | QF32 |
| shvp-qf33 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.QF33_FbMcbIbpReserve` | Питание БУТ от ИБП | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 2 | QF33 |
| shvp-qf41 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.QF41_FbMcbBpUZ1` | Питание источника 24 В UZ1 | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 3 | QF41 |
| shvp-qf42 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.QF42_FbMcbBp2A1` | Питание модулей ПЛК | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 4 | QF42 |
| shvp-u51 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x2A3DigitalInput.U51_FbBriz2` | Контроль БРИЗ модуля 2A5 DO | Boolean | Chanel1 | ШВП ПМ | 2A3 | DI | бит 7 | U51 |
| ch1-10a3-k1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.K1Ch1_FbRtp1` | Контроль общего реле аварии ТП1 | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 19 | K1 |
| ch1-10a3-k2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.K2Ch1_FbRtp2` | Контроль реле напряжения и фаз | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 20 | K2 |
| ch1-10a3-k200 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.K200Ch1_FbFltCpuCh2` | Контроль аварии канала 2 | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 21 | K200 |
| ch1-10a3-sprut-block | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SprutCh1_Blokirovka` | Блокировка от канала 1 тормозной системы | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 10 | БУТ |
| ch1-10a3-sq1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ1_SinhLeftTop` | Синхронизация левого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 0 | SQ1 |
| ch1-10a3-sq11 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ11_ProvStrLeviy` | Провисание каната левого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 24 | SQ11 |
| ch1-10a3-sq12 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ12_ProvStrPraviy` | Провисание каната правого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 25 | SQ12 |
| ch1-10a3-sq3 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ3_SinhRightTop` | Синхронизация правого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 2 | SQ3 |
| ch1-10a3-sq5 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ5_OwerWindLeft` | Переподъём левого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 4 | SQ5 |
| ch1-10a3-sq6 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.SQ6_OwerWindRight` | Переподъём правого сосуда | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 5 | SQ6 |
| ch1-10a3-u20 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.U20Ch1_FbBriz2` | Контроль БРИЗ: внутренние цепи 24 В и реле K1 | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 16 | U20 |
| ch1-10a3-u21 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.U21Ch1_FbBriz2` | Контроль БРИЗ: барьеры и питание энкодеров | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 17 | U21 |
| ch1-10a3-u22 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A3DigitalInput.U22Ch1_FbBriz2` | Контроль БРИЗ внешних устройств | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A3 | DI | бит 18 | U22 |
| ch1-10a4-u23-nc | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A4DigitalInput.U23_RkiNc` | Контроль изоляции, контакт NC | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A4 | DI | — | U23 |
| ch1-10a4-u23-no | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A4DigitalInput.U23_RkiNo` | Контроль изоляции, контакт NO | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A4 | DI | — | U23 |
| ch1-10a4-uz1 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A4DigitalInput.UZ1_FltBp1` | Контроль блока питания 1 | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A4 | DI | — | UZ3 |
| ch1-10a4-uz2 | `ns=2;s=Chanel1.Application.GVL.GlobalInOutSignal.x10A4DigitalInput.UZ2_FltBp2` | Контроль блока питания 2 | Boolean | Chanel1 | ШУ ПМ, канал 1 | 10A4 | DI | — | UZ4 |
| ch1-encoder-1 | `ns=2;s=Chanel1.Application.GVL.GVL.ParametrsEncoder1.impCounter` | Импульсы энкодера прямого вала | Int16 | Chanel1 | ШУ ПМ, канал 1 | 10A4 | Counter | имп. | BR1.1 |
| ch1-encoder-2 | `ns=2;s=Chanel1.Application.GVL.GVL.ParametrsEncoder2.impCounter` | Импульсы второго энкодера прямого вала | Int16 | Chanel1 | ШУ ПМ, канал 1 | 10A5 | Counter | имп. | BR1.2 |
| ch2-20a3-k2 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.K2Ch2_FbRtp2` | Контроль общего реле аварии ТП2 | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 20 | K2 |
| ch2-20a3-sq1 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.SQ1_SinhLeftTop` | Синхронизация левого сосуда | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 0 | SQ1 |
| ch2-20a3-sq3 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.SQ3_SinhRightTop` | Синхронизация правого сосуда | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 2 | SQ3 |
| ch2-20a3-sq5 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.SQ5_OwerWindLeft` | Переподъём левого сосуда | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 4 | SQ5 |
| ch2-20a3-sq6 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.SQ6_OwerWindRight` | Переподъём правого сосуда | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 5 | SQ6 |
| ch2-20a3-u10 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.U10Ch2_FbBriz2` | Контроль БРИЗ внутренних цепей 24 В | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 16 | U10 |
| ch2-20a3-u11 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.U11Ch2_FbBriz2` | Контроль БРИЗ барьеров и энкодеров | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 17 | U11 |
| ch2-20a3-u12 | `ns=2;s=Chanel2.Application.GVL.GlobalInOutSignal.x20A3DigitalInput.U12Ch2_FbBriz2` | Контроль БРИЗ внешних устройств | Boolean | Chanel2 | ШУ ПМ, канал 2 | 20A3 | DI | бит 18 | U12 |
| ch2-encoder-1 | `ns=2;s=Chanel2.Application.GVL.GVL.ParametrsEncoder1.impCounter` | Импульсы энкодера фрикционного ролика | Int16 | Chanel2 | ШУ ПМ, канал 2 | 20A4 | Counter | имп. | BR2.1 |
| ch2-encoder-2 | `ns=2;s=Chanel2.Application.GVL.GVL.ParametrsEncoder2.impCounter` | Импульсы второго энкодера фрикционного ролика | Int16 | Chanel2 | ШУ ПМ, канал 2 | 20A5 | Counter | имп. | BR1.2 |
