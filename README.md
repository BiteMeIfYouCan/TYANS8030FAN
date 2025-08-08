# TYANS8030FAN

一个泰安S8030-2t基于BMC调速的脚本
脚本共调整了四个风扇，分别是：

| 传感器名称                          | 主板4pin接口 | 物理位置                         | IPMI ID | 脚本划分 |
| ----------------------------------- | ------------ | -------------------------------- | ------- | -------- |
| CPU_FAN                             | J34 CPU_FAN  | CPU 插座右上角、8-pin EPS 电源旁 | 0x00    | CPU      |
| SYS_FAN_1                           | J17 SYS_FAN1 | 主板底边最右侧、24-pin ATX 旁    | 0x02    | 机箱风扇 |
| SYS_FAN_2                           | J21 SYS_FAN2 | 底边靠近电池位置                 | 0x03    | 硬盘风扇 |
| SYS_FAN_3                           | J18 SYS_FAN3 | 底边靠左、靠近芯片组散热片       | 0x04    | PCIE风扇 |
| SYS_FAN_4（理论上是的，但是我没用） | J42 FAN_FP   | 需接配套转接线                   | 0x05    | 没写     |

![OIP](https://github.com/user-attachments/assets/c22dcecc-78c2-4465-8da9-1e041163bf96)



### 注意事项：

> 脚本由CHATGPT生成，可以随意更改，本脚本仅在 pve 9.0 ， S8030GM4NE-2T + epyc 7302p + lsi 9361-8i 上测试通过 ，其他的平台不保证测试通过
>
> 机箱风扇关联的pcie和cpu风扇，机箱风扇被软限制为最高转速50%，当机箱内温度确实特别高时，才会提升机箱风扇



#### 感谢以下大佬分享方案：

> chiphell 的 wangmice：[今天无意中发现泰安S8030通过ipmi控制风扇速度的命令，记录一下](https://www.chiphell.com/thread-2604921-1-1.html)
>
> github 的 sonmihpc：[sonmihpc/AutoFan: 适配泰安S8030显卡机自动调节系统风扇组件](https://github.com/sonmihpc/AutoFan)
>
> 感谢本世纪最伟大的工具：ChatGPT
