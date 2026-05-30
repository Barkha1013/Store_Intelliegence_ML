import { Router, type IRouter } from "express";
import healthRouter from "./health";
import storesRouter from "./stores";

const router: IRouter = Router();

router.use(healthRouter);
router.use(storesRouter);

export default router;
