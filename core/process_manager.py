#!/usr/bin/env python3
"""Global process manager - track and control running warmup/posting processes"""

import threading
import time
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)


class ManagedProcess:
    """Represents a running background process"""
    
    def __init__(self, process_id: str, description: str, thread: threading.Thread = None):
        self.id = process_id
        self.description = description
        self.thread = thread
        self.started_at = time.time()
        self._stop_flag = threading.Event()
        self.status = "running"
    
    def stop(self):
        """Signal this process to stop"""
        self._stop_flag.set()
        self.status = "stopping"
        logger.info(f"⏹ Stop signal sent to: {self.description}")
    
    def is_stopped(self) -> bool:
        """Check if stop was requested"""
        return self._stop_flag.is_set()
    
    def elapsed_str(self) -> str:
        """Human-readable elapsed time"""
        elapsed = int(time.time() - self.started_at)
        if elapsed < 60:
            return f"{elapsed}с"
        elif elapsed < 3600:
            return f"{elapsed // 60}м {elapsed % 60}с"
        else:
            return f"{elapsed // 3600}ч {(elapsed % 3600) // 60}м"
    
    def to_dict(self) -> dict:
        """Serialize for API"""
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "elapsed": self.elapsed_str(),
        }


class ProcessManager:
    """Manages all running processes (warmup, posting, etc.)"""
    
    def __init__(self):
        self._processes: Dict[str, ManagedProcess] = {}
        self._lock = threading.Lock()
        self._next_id = 1
    
    def start(self, description: str, target, args=(), kwargs=None) -> ManagedProcess:
        """Start a new background process"""
        with self._lock:
            pid = f"p{self._next_id}"
            self._next_id += 1
            
            proc = ManagedProcess(pid, description)
            
            # Wrap target to handle the stop flag
            def wrapped_target(stop_flag, *w_args, **w_kwargs):
                try:
                    # Pass stop_flag as keyword argument
                    w_kwargs['stop_flag'] = stop_flag
                    target(*w_args, **w_kwargs)
                except Exception as e:
                    logger.error(f"Process {pid} ({description}) failed: {e}")
            
            thread = threading.Thread(
                target=wrapped_target,
                args=(proc._stop_flag, *args),
                kwargs=kwargs or {},
                daemon=True,
            )
            proc.thread = thread
            thread.start()
            
            self._processes[pid] = proc
            logger.info(f"▶ Started process {pid}: {description}")
            return proc
    
    def stop(self, process_id: str) -> bool:
        """Stop a specific process"""
        with self._lock:
            proc = self._processes.get(process_id)
            if proc:
                proc.stop()
                return True
            return False
    
    def stop_all(self) -> int:
        """Stop all running processes"""
        count = 0
        with self._lock:
            for proc in self._processes.values():
                if proc.status == "running":
                    proc.stop()
                    count += 1
        logger.info(f"⏹ Stopped {count} processes")
        return count
    
    def get_process(self, process_id: str) -> ManagedProcess:
        """Get process by ID"""
        return self._processes.get(process_id)
    
    def list_processes(self) -> List[dict]:
        """Get list of all active processes"""
        # Clean up finished processes
        with self._lock:
            to_remove = []
            for pid, proc in self._processes.items():
                if proc.thread and not proc.thread.is_alive():
                    proc.status = "completed"
                    to_remove.append(pid)
            
            for pid in to_remove:
                logger.info(f"Process {pid}: completed, removing from list")
                del self._processes[pid]
        
        # Return active processes
        with self._lock:
            return [p.to_dict() for p in self._processes.values()]
    
    def is_running(self, process_id: str) -> bool:
        """Check if a process is still running"""
        proc = self._processes.get(process_id)
        if not proc:
            return False
        return proc.thread is not None and proc.thread.is_alive()
