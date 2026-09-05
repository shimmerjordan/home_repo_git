"""标注语句集 —— 用来量"AI 到底准不准", 而不是靠感觉。

每条 case:
  id       稳定标识 (失败明细里靠它定位)
  text     用户说的话
  cat      分类, 报告按分类出准确率
  ops      期望的操作列表, **顺序无关**。每条:
             intent  期望意图
             item    期望落到 fixture 里哪个物品 (按名字; 同名多处用 loc 区分)
             loc     可选, 物品所在位置名 (电池在两处都有时用)
             new     期望"新建"而不是匹配已有 (值 = 期望的新物品名)
             skip    期望这条无法执行 (库里没有且不能新建)
             qty     期望数量
             to      可选, put_in/create_item 的目标位置名
  只读 (find/assist) 的 case 用 ops 里的 intent="find" 表达。

  decisions  可选。有它就在 plan 之后再跑一次 apply (端到端 plan → decisions →
             apply)。每条 {intent, option_key, new_item_name?, location_name?,
             quantity?}, option_key ∈ {"new", "skip", "i:<fixture物品名>"}。
             **写 case 时用物品名而不是 id** —— fixture 每个 case 都重新建库,
             物品 id 不稳定。同名多处 (比如"电池"在书桌1和洗漱柜各一份) 用
             "物品名@位置名" 消歧, run_once 会在跑之前把它翻译成 "i:<真实id>"。
  after      可选。apply 之后对库的断言, 每条 {item, loc?, qty?, exists?}。
             item 同样支持 "物品名@位置名" 写法; qty 断言数量, exists=False
             断言物品已被删除 (比如 delete_item 之后)。

分类的意义: 用户抱怨的是"多物品操作经常不符合预期", 所以 multi / mixed 两类的
准确率是这次优化的主要 KPI; confusable 类盯的是"存入新物品被错误合并"。
"""

CASES = [
    # ---- 单物品基本操作 ----
    dict(id="single-find", cat="single", text="充电宝在哪",
         ops=[dict(intent="find", item="充电宝")]),
    dict(id="single-take", cat="single", text="我拿了螺丝刀",
         ops=[dict(intent="take_out", item="螺丝刀", qty=1)]),
    dict(id="single-consume", cat="single", text="我用完了一瓶洗手液",
         ops=[dict(intent="consume", item="洗手液", qty=1)]),
    dict(id="single-putin", cat="single", text="把卷尺放回书房",
         ops=[dict(intent="put_in", item="卷尺", qty=1, to="书房")]),
    dict(id="single-qty", cat="single", text="拿三个电池",
         ops=[dict(intent="take_out", item="电池", qty=3)]),
    dict(id="single-alias", cat="single", text="移动电源在哪",
         ops=[dict(intent="find", item="充电宝")]),

    # ---- 多物品 (同一种意图) ----
    dict(id="multi-find2", cat="multi", text="螺丝刀和卷尺在哪",
         ops=[dict(intent="find", item="螺丝刀"), dict(intent="find", item="卷尺")]),
    dict(id="multi-take2", cat="multi", text="我拿了螺丝刀和卷尺",
         ops=[dict(intent="take_out", item="螺丝刀", qty=1),
              dict(intent="take_out", item="卷尺", qty=1)]),
    dict(id="multi-putin3", cat="multi", text="把手表、铅笔、橡皮放进书桌1",
         ops=[dict(intent="put_in", new="手表", qty=1, to="书桌1"),
              dict(intent="put_in", new="铅笔", qty=1, to="书桌1"),
              dict(intent="put_in", new="橡皮", qty=1, to="书桌1")]),
    dict(id="multi-qty2", cat="multi", text="用了两个电池和一瓶洗手液",
         ops=[dict(intent="consume", item="电池", qty=2),
              dict(intent="consume", item="洗手液", qty=1)]),
    dict(id="multi-take3", cat="multi", text="拿走螺丝刀、卷尺和充电器",
         ops=[dict(intent="take_out", item="螺丝刀", qty=1),
              dict(intent="take_out", item="卷尺", qty=1),
              dict(intent="take_out", item="充电器", qty=1)]),
    dict(id="multi-putin5", cat="multi", text="把手表、铅笔、橡皮、订书机、计算器放进书桌1",
         ops=[dict(intent="put_in", new="手表", to="书桌1"),
              dict(intent="put_in", new="铅笔", to="书桌1"),
              dict(intent="put_in", new="橡皮", to="书桌1"),
              dict(intent="put_in", new="订书机", to="书桌1"),
              dict(intent="put_in", new="计算器", to="书桌1")]),

    # ---- 混合意图 (最难的一类) ----
    dict(id="mixed-take-put", cat="mixed", text="拿了卷尺, 顺便把螺丝刀放回工具箱",
         ops=[dict(intent="take_out", item="卷尺", qty=1),
              dict(intent="put_in", item="螺丝刀", qty=1, to="工具箱")]),
    dict(id="mixed-4", cat="mixed",
         text="我用完了洗手液, 拿了螺丝刀和卷尺, 顺便把两个充电器放回书桌1",
         ops=[dict(intent="consume", item="洗手液", qty=1),
              dict(intent="take_out", item="螺丝刀", qty=1),
              dict(intent="take_out", item="卷尺", qty=1),
              dict(intent="put_in", item="充电器", qty=2, to="书桌1")]),
    dict(id="mixed-find-take", cat="mixed", text="充电宝在哪? 另外帮我拿一个电池",
         ops=[dict(intent="find", item="充电宝"),
              dict(intent="take_out", item="电池", qty=1)]),
    dict(id="mixed-consume-take", cat="mixed", text="洗手液用完了, 螺丝刀我先借走",
         ops=[dict(intent="consume", item="洗手液", qty=1),
              dict(intent="take_out", item="螺丝刀", qty=1)]),

    # ---- 易混匹配 (存入新物品不能被合并到名字相近的已有物品) ----
    dict(id="conf-shampoo", cat="confusable", text="把洗发水放进洗漱柜",
         ops=[dict(intent="put_in", new="洗发水", qty=1, to="洗漱柜")]),
    dict(id="conf-cable", cat="confusable", text="把充电线放进书桌1",
         ops=[dict(intent="put_in", new="充电线", qty=1, to="书桌1")]),
    dict(id="conf-screw-vs-driver", cat="confusable", text="拿一把螺丝刀",
         ops=[dict(intent="take_out", item="螺丝刀", qty=1)]),
    dict(id="conf-screw", cat="confusable", text="用了五个螺丝",
         ops=[dict(intent="consume", item="螺丝", qty=5)]),
    dict(id="conf-charger-vs-bank", cat="confusable", text="把充电器放回书桌1",
         ops=[dict(intent="put_in", item="充电器", qty=1, to="书桌1")]),

    # ---- 新增 = 全新物品, 不匹配 ----
    dict(id="new-explicit", cat="force_new", text="新增一个香薰到洗漱柜",
         ops=[dict(intent="create_item", new="香薰", qty=1, to="洗漱柜")]),
    dict(id="new-same-name", cat="force_new", text="新增充电宝到书桌1",
         ops=[dict(intent="create_item", new="充电宝", qty=1, to="书桌1")]),
    dict(id="new-add-verb", cat="force_new", text="添加一把美工刀到工具箱",
         ops=[dict(intent="create_item", new="美工刀", qty=1, to="工具箱")]),
    dict(id="new-record", cat="force_new", text="帮我录入一个体温计, 放卫生间",
         ops=[dict(intent="create_item", new="体温计", qty=1, to="卫生间")]),

    # ---- 补货: 是补已有档案, 不是新增 ----
    dict(id="restock-depleted", cat="restock", text="又买了两包抽纸, 放洗漱柜",
         ops=[dict(intent="put_in", item="抽纸", qty=2, to="洗漱柜")]),
    dict(id="restock-existing", cat="restock", text="给电池补货, 再放四个到书桌1",
         ops=[dict(intent="put_in", item="电池", loc="书桌1", qty=4, to="书桌1")]),

    # ---- 同名多处 ----
    dict(id="ambig-battery", cat="ambiguous", text="电池在哪",
         ops=[dict(intent="find", item="电池")]),

    # ---- 库里没有且不能新建 ----
    dict(id="absent-take", cat="absent", text="我拿了吹风机",
         ops=[dict(intent="take_out", skip=True, qty=1)]),
    dict(id="absent-consume", cat="absent", text="我把咖啡喝完了",
         ops=[dict(intent="consume", skip=True, qty=1)]),

    # ---- 只读: 查询与需求推荐 ----
    dict(id="read-find", cat="readonly", text="工具箱里有什么",
         ops=[], expect_readonly=True),
    dict(id="read-assist", cat="readonly", text="我想拧个螺丝, 家里有什么能用的",
         ops=[], expect_readonly=True),

    # ---- 确认后落库 (端到端 plan → decisions → apply) ----
    dict(id="apply-takeout", cat="apply", text="拿两个电池",
         ops=[dict(intent="take_out", item="电池", qty=2)],
         decisions=[dict(intent="take_out", option_key="i:电池@书桌1", quantity=2)],
         after=[dict(item="电池", loc="书桌1", qty=6)]),

    # ---- 删除物品档案 ----
    dict(id="del-single", cat="delete", text="把螺丝刀这条记录删掉",
         ops=[dict(intent="delete_item", item="螺丝刀")]),
    dict(id="del-multi", cat="delete", text="把卷尺和螺丝刀的记录都删了",
         ops=[dict(intent="delete_item", item="卷尺"),
              dict(intent="delete_item", item="螺丝刀")]),
    dict(id="del-absent", cat="delete", text="把跑步机的记录删掉",
         ops=[dict(intent="delete_item", skip=True)]),

    # ---- 确认后落库 ----
    dict(id="apply-consume", cat="apply", text="用完了一瓶洗手液",
         ops=[dict(intent="consume", item="洗手液", qty=1)],
         decisions=[dict(intent="consume", option_key="i:洗手液", quantity=1)],
         after=[dict(item="洗手液", qty=0)]),
    dict(id="apply-new", cat="apply", text="新增一个订书机放到书桌1",
         ops=[dict(intent="create_item", new="订书机", qty=1, to="书桌1")],
         decisions=[dict(intent="create_item", option_key="new",
                         new_item_name="订书机", location_name="书桌1", quantity=1)],
         after=[dict(item="订书机", loc="书桌1", qty=1)]),
    dict(id="apply-delete", cat="delete", text="把卷尺这条记录删掉",
         ops=[dict(intent="delete_item", item="卷尺")],
         decisions=[dict(intent="delete_item", option_key="i:卷尺")],
         after=[dict(item="卷尺", exists=False)]),

    # ---- 多物品句里的 force_new 归属 ----
    dict(id="fnmulti-two", cat="forcenew_multi", text="新增手表和铅笔到书桌1",
         ops=[dict(intent="create_item", new="手表", to="书桌1"),
              dict(intent="create_item", new="铅笔", to="书桌1")]),
    dict(id="fnmulti-mixed", cat="forcenew_multi",
         text="新增一个订书机到书桌1, 再把卷尺也放进去",
         ops=[dict(intent="create_item", new="订书机", to="书桌1"),
              dict(intent="put_in", item="卷尺", to="书桌1")]),

    # ---- 量词 ----
    dict(id="qty-dozen", cat="quantifier", text="拿一打电池",
         ops=[dict(intent="take_out", item="电池", qty=12)]),
    dict(id="qty-pair", cat="quantifier", text="用了两双手套",
         ops=[dict(intent="consume", skip=True)]),
    dict(id="qty-half", cat="quantifier", text="用了半瓶洗手液",
         ops=[dict(intent="consume", item="洗手液", qty=1)]),

    # ---- 否定 ----
    dict(id="neg-single", cat="negation", text="拿卷尺, 别拿螺丝刀",
         ops=[dict(intent="take_out", item="卷尺", qty=1)]),
    dict(id="neg-except", cat="negation", text="把书桌1 上的东西都拿走, 除了电池",
         xfail="复杂否定: 要先列出书桌1 上的全部物品再减去电池, 模型目前会退化成一条 skip。"
               "留着这条是为了量出差距, 修好了就把 xfail 去掉。",
         ops=[dict(intent="take_out", item="充电宝", qty=1),
              dict(intent="take_out", item="充电器", qty=1)]),

    # ---- 位置歧义 ----
    dict(id="locambig-exact", cat="locambig", text="把卷尺放进书桌1",
         ops=[dict(intent="put_in", item="卷尺", to="书桌1")]),
]
